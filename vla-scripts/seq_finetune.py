"""
seq_finetune.py

Script for sequentially parameter-efficient fine-tuning of OpenVLA models loaded through the HuggingFace AutoClasses, using
HuggingFace PEFT library for low-rank adaptation (LoRA).

Sequentially fine-tunes OpenVLA on a number of datasets

Changed from the original script: finetune.py

Notes & Benchmarks:
    - Requires PEFT (`pip install peft==0.11.1`)
    - LoRA fine-tuning (see parameters below -- no quantization, LoRA rank = 32, target_modules = all-linear):
        + One 48 GB GPU can fit a Batch Size of 12
        + One 80 GB GPU can fit a Batch Size of 24

Run with:
    - [Single Node Multi-GPU (= $K) ]: torchrun --standalone --nnodes 1 --nproc-per-node $K vla-scripts/finetune.py
    - [Override Config Values]: torchrun --standalone --nnodes 1 --nproc-per-node $K vla-scripts/finetune.py \
                                    --data_root_dir <PATH/TO/RLDS/DATASETS/DIRECTORY> \
                                    --dataset_name <DATASET_NAME> \
                                    --run_root_dir <PATH/TO/LOGS/DIR> \
                                    ...
        
    CUDA_VISIBLE_DEVICES=0,1,2,3 torchrun --standalone --nnodes 1 --nproc-per-node 4 vla-scripts/finetune.py \
        --data_root_dir /data2/zhaoyu/LIBERO_rlds/ \
        --dataset_name libero_spatial \
        --run_root_dir /data2/zhaoyu/LIBERO_finetune/logs/libero_spatial \
        --adapter_tmp_dir /data2/zhaoyu/LIBERO_finetune/checkpoints/libero_spatial
"""

import os
os.environ['TRANSFORMERS_CACHE'] = '/data2/zhaoyu/huggingface_cache'
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
import datetime

import draccus
import torch
import torch.distributed as dist
import tqdm
from accelerate import PartialState
from peft import LoraConfig, PeftModel, get_peft_model, prepare_model_for_kbit_training
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.optim import AdamW
from torch.utils.data import DataLoader
from transformers import AutoModelForVision2Seq, AutoProcessor, BitsAndBytesConfig
from transformers.modeling_outputs import CausalLMOutputWithPast

import wandb
from prismatic.models.backbones.llm.prompting import PurePromptBuilder, VicunaV15ChatPromptBuilder
from prismatic.util.data_utils import PaddedCollatorForActionPrediction
from prismatic.vla.action_tokenizer import ActionTokenizer
from prismatic.vla.datasets import RLDSBatchTransform, RLDSDataset
from prismatic.vla.datasets.rlds.utils.data_utils import save_dataset_statistics

# Sane Defaults
os.environ["TOKENIZERS_PARALLELISM"] = "false"

# For NUS LinSLab server better performance
torch.set_num_threads(6)

# # === Utilities ===
# # fmt: off
# def create_vision_transform(vla: nn.Module, input_size: int) -> Callable[[Image.Image], torch.Tensor]:
#     """Gets image transform for the vision encoder."""
#     data_cfg = timm.data.resolve_model_data_config(vla.vision_backbone)
#     data_cfg["input_size"] = (3, input_size, input_size)
#     return timm.data.create_transform(
#         input_size=data_cfg["input_size"],
#         interpolation=data_cfg["interpolation"],
#         mean=data_cfg["mean"],
#         std=data_cfg["std"],
#         crop_pct=1.0,           # Set to 1.0 to disable cropping
#         crop_mode="center",     # Default crop mode --> no-op when `crop_pct == 1.0`
#         is_training=False,      # Disable image_aug when loading transform; handled by RLDS dataloader
#     )
#
# # fmt: on


@dataclass
class FinetuneConfig:
    # fmt: off
    vla_path: str = "openvla/openvla-7b"                                                # Path to OpenVLA model (on HuggingFace Hub) to start with

    # Directory Paths
    data_root_dir: Path = Path("/data2/zhaoyu/LIBERO_spatial_rlds_single")              # Path to parent directory for all RLDS datasets
    dataset_names: list = field(default_factory=lambda: [
        "libero_spatial_0",
        "libero_spatial_1",
        "libero_spatial_2",
        "libero_spatial_3",
        "libero_spatial_4",
        "libero_spatial_5",
        "libero_spatial_6",
        "libero_spatial_7",
        "libero_spatial_8",
        "libero_spatial_9"
    ])                                                                              # Name of fine-tuning datasets in sequence, they should be under `data_root_dir`
    run_root_dir: Path = Path("/data2/zhaoyu/openvla_seq_finetune/data_dir")            # Path to directory to store logs & checkpoints
    adapter_tmp_dir: Path = Path("/data2/zhaoyu/openvla_seq_finetune/adapter_temp")     # Temporary directory for LoRA weights before fusing

    # Fine-tuning Parameters
    batch_size: int = 2                                                                 # Fine-tuning batch size
    max_steps: int = 20000                                                              # Max number of fine-tuning steps for each dataset (total steps = max_steps * len(dataset_names))
    save_steps: int = 20000                                                             # Interval for checkpoint saving
    learning_rate: float = 2e-5                                                         # Fine-tuning learning rate
    grad_accumulation_steps: int = 1                                                    # Gradient accumulation steps
    image_aug: bool = False                                                             # Whether to train with image augmentations
    shuffle_buffer_size: int = 1_000                                                    # Dataloader shuffle buffer size (can reduce if OOM)

    # LoRA Arguments
    use_lora: bool = True                                                               # Whether to use LoRA fine-tuning
    lora_rank: int = 32                                                                 # Rank of LoRA weight matrix
    lora_dropout: float = 0.0                                                           # Dropout applied to LoRA weights
    use_quantization: bool = False                                                      # Whether to 4-bit quantize VLA for LoRA fine-tuning
                                                                                        #   => CAUTION: Reduces memory but hurts performance

    # Tracking Parameters
    wandb_project: str = "openvla_seq"                                                  # Name of W&B project to log to (use default!)
    wandb_entity: str = "object814-national-university-of-singapore"                    # Name of entity to log under

    # fmt: on


@draccus.wrap()
def seq_finetune(cfg: FinetuneConfig) -> None:
    print(f"Sequentially fine-tuning OpenVLA Model `{cfg.vla_path}`")

    # Initialize W&B once for the entire run
    if torch.distributed.get_rank() == 0:
        wandb.init(entity=cfg.wandb_entity, project=cfg.wandb_project, name="seq_finetune")

    for dataset_name in cfg.dataset_names:
        print(f"Fine-tuning on `{dataset_name}`")

        # [Validate] Ensure GPU Available & Set Device / Distributed Context
        assert torch.cuda.is_available(), "Fine-tuning assumes at least one GPU is available!"
        distributed_state = PartialState()
        torch.cuda.set_device(device_id := distributed_state.local_process_index)
        torch.cuda.empty_cache()

        # Configure Unique Experiment ID & Log Directory
        exp_id = (
            f"{dataset_name}"
            f"+b{cfg.batch_size * cfg.grad_accumulation_steps}"
            f"+lr-{cfg.learning_rate}"
            f"+date-{datetime.datetime.now().strftime('%Y%m%d')}"
        )
        if cfg.use_lora:
            exp_id += f"+lora-r{cfg.lora_rank}+dropout-{cfg.lora_dropout}"
        if cfg.use_quantization:
            exp_id += "+q-4bit"

        # Start =>> Build Directories
        run_dir, adapter_dir = cfg.run_root_dir / exp_id, cfg.adapter_tmp_dir / exp_id
        os.makedirs(run_dir, exist_ok=True)

        # Quantization Config =>> only if LoRA fine-tuning
        quantization_config = None
        if cfg.use_quantization:
            assert cfg.use_lora, "Quantized training only supported for LoRA fine-tuning!"
            quantization_config = BitsAndBytesConfig(
                load_in_4bit=True, bnb_4bit_compute_dtype=torch.bfloat16, bnb_4bit_quant_type="nf4"
            )

        # Load OpenVLA Processor and Model using HF AutoClasses
        processor = AutoProcessor.from_pretrained(cfg.vla_path, trust_remote_code=True)
        vla = AutoModelForVision2Seq.from_pretrained(
            cfg.vla_path,
            torch_dtype=torch.bfloat16,
            quantization_config=quantization_config,
            low_cpu_mem_usage=True,
            trust_remote_code=True,
        )

        # Device Placement =>> note that BitsAndBytes automatically handles for quantized training
        if cfg.use_quantization:
            vla = prepare_model_for_kbit_training(vla)
        else:
            vla = vla.to(device_id)

        # [LoRA] Wrap Model w/ PEFT `LoraConfig` =>> by default we set `target_modules=all-linear`
        if cfg.use_lora:
            lora_config = LoraConfig(
                r=cfg.lora_rank,
                lora_alpha=min(cfg.lora_rank, 16),
                lora_dropout=cfg.lora_dropout,
                target_modules="all-linear",
                init_lora_weights="gaussian",
            )
            vla = get_peft_model(vla, lora_config)
            vla.print_trainable_parameters()

        # Wrap VLA in PyTorch DDP Wrapper for Multi-GPU Training
        vla = DDP(vla, device_ids=[device_id], find_unused_parameters=True, gradient_as_bucket_view=True)

        # Create Optimizer =>> note that we default to a simple constant learning rate!
        trainable_params = [param for param in vla.parameters() if param.requires_grad]
        optimizer = AdamW(trainable_params, lr=cfg.learning_rate)

        # Create Action Tokenizer
        action_tokenizer = ActionTokenizer(processor.tokenizer)

        # Load Fine-tuning Dataset
        batch_transform = RLDSBatchTransform(
            action_tokenizer,
            processor.tokenizer,
            image_transform=processor.image_processor.apply_transform,
            prompt_builder_fn=PurePromptBuilder if "v01" not in cfg.vla_path else VicunaV15ChatPromptBuilder,
        )
        vla_dataset = RLDSDataset(
            cfg.data_root_dir,
            dataset_name,
            batch_transform,
            resize_resolution=tuple(vla.module.config.image_sizes),
            shuffle_buffer_size=cfg.shuffle_buffer_size,
            image_aug=cfg.image_aug,
        )

        # [Important] Save Dataset Statistics =>> used to de-normalize actions for inference!
        if distributed_state.is_main_process:
            save_dataset_statistics(vla_dataset.dataset_statistics, run_dir)

        # Create Collator and DataLoader
        collator = PaddedCollatorForActionPrediction(
            processor.tokenizer.model_max_length, processor.tokenizer.pad_token_id, padding_side="right"
        )
        dataloader = DataLoader(
            vla_dataset,
            batch_size=cfg.batch_size,
            sampler=None,
            collate_fn=collator,
            num_workers=0,  # Important =>> Set to 0 if using RLDS; TFDS rolls its own parallelism!
        )

        # Deque to store recent train metrics (used for computing smoothened metrics for gradient accumulation)
        recent_losses = deque(maxlen=cfg.grad_accumulation_steps)
        recent_action_accuracies = deque(maxlen=cfg.grad_accumulation_steps)
        recent_l1_losses = deque(maxlen=cfg.grad_accumulation_steps)

        # Initialize step counter
        step_counter = 0

        # Train!
        with tqdm.tqdm(total=cfg.max_steps, leave=True) as progress:
            vla.train()
            optimizer.zero_grad()
            for batch_idx, batch in enumerate(dataloader):
                with torch.autocast("cuda", dtype=torch.bfloat16):
                    output: CausalLMOutputWithPast = vla(
                        input_ids=batch["input_ids"].to(device_id),
                        attention_mask=batch["attention_mask"].to(device_id),
                        pixel_values=batch["pixel_values"].to(torch.bfloat16).to(device_id),
                        labels=batch["labels"],
                    )
                    loss = output.loss

                # Normalize loss to account for gradient accumulation
                normalized_loss = loss / cfg.grad_accumulation_steps

                # Backward pass
                normalized_loss.backward()

                # Compute Accuracy and L1 Loss for Logging
                action_logits = output.logits[:, vla.module.vision_backbone.featurizer.patch_embed.num_patches : -1]
                action_preds = action_logits.argmax(dim=2)
                action_gt = batch["labels"][:, 1:].to(action_preds.device)
                mask = action_gt > action_tokenizer.action_token_begin_idx

                # Compute Accuracy
                correct_preds = (action_preds == action_gt) & mask
                action_accuracy = correct_preds.sum().float() / mask.sum().float()

                # Compute L1 Loss on Predicted (Continuous) Actions
                continuous_actions_pred = torch.tensor(
                    action_tokenizer.decode_token_ids_to_actions(action_preds[mask].cpu().numpy())
                )
                continuous_actions_gt = torch.tensor(
                    action_tokenizer.decode_token_ids_to_actions(action_gt[mask].cpu().numpy())
                )
                action_l1_loss = torch.nn.functional.l1_loss(continuous_actions_pred, continuous_actions_gt)

                # Store recent train metrics
                recent_losses.append(loss.item())
                recent_action_accuracies.append(action_accuracy.item())
                recent_l1_losses.append(action_l1_loss.item())

                # Compute gradient step index
                gradient_step_idx = batch_idx // cfg.grad_accumulation_steps

                # Compute smoothened train metrics
                smoothened_loss = sum(recent_losses) / len(recent_losses)
                smoothened_action_accuracy = sum(recent_action_accuracies) / len(recent_action_accuracies)
                smoothened_l1_loss = sum(recent_l1_losses) / len(recent_l1_losses)

                # Push Metrics to W&B (every 10 gradient steps)
                if distributed_state.is_main_process and gradient_step_idx % 10 == 0:
                    wandb.log(
                        {
                            f"{dataset_name}/train_loss": smoothened_loss,
                            f"{dataset_name}/action_accuracy": smoothened_action_accuracy,
                            f"{dataset_name}/l1_loss": smoothened_l1_loss,
                        },
                        step=gradient_step_idx,
                    )

                # Optimizer Step
                if (batch_idx + 1) % cfg.grad_accumulation_steps == 0:
                    optimizer.step()
                    optimizer.zero_grad()
                    progress.update(cfg.grad_accumulation_steps)
                    step_counter += cfg.grad_accumulation_steps

                # Save Model Checkpoint =>> by default, only keeps the latest checkpoint, continually overwriting it!
                if gradient_step_idx > 0 and (gradient_step_idx+1) % cfg.save_steps == 0:
                    if distributed_state.is_main_process:
                        print(f"Saving Model Checkpoint for Step {gradient_step_idx+1}")

                        # If LoRA, we first save adapter weights, then merge into full model; otherwise, default save!
                        save_dir = adapter_dir if cfg.use_lora else run_dir

                        # Save Processor & Weights
                        processor.save_pretrained(run_dir)
                        vla.module.save_pretrained(save_dir)

                    # Wait for processor and adapter weights to be saved by main process
                    dist.barrier()

                    # Merge LoRA weights into model backbone for faster inference
                    #   =>> Note that merging is slow and can be done post-hoc to speed up training
                    if cfg.use_lora:
                        base_vla = AutoModelForVision2Seq.from_pretrained(
                            cfg.vla_path, torch_dtype=torch.bfloat16, low_cpu_mem_usage=True, trust_remote_code=True
                        )
                        merged_vla = PeftModel.from_pretrained(base_vla, adapter_dir)
                        merged_vla = merged_vla.merge_and_unload()
                        if distributed_state.is_main_process:
                            merged_vla.save_pretrained(run_dir)

                    # Block on Main Process Checkpointing
                    dist.barrier()
                    
                if step_counter >= cfg.max_steps:
                    break

        # Update vla_path for the next dataset
        cfg.vla_path = str(run_dir)

if __name__ == "__main__":
    seq_finetune()