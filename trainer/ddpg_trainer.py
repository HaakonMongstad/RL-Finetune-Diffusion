import os
import warnings
from collections import defaultdict
from concurrent import futures
from typing import Any, Callable, Optional, Tuple
from warnings import warn

import torch
import torch.nn.functional as F
from accelerate import Accelerator
from accelerate.logging import get_logger
from accelerate.utils import ProjectConfiguration, set_seed
from huggingface_hub import whoami
from trl.models import DDPOStableDiffusionPipeline
from trl.trainer import BaseTrainer, DDPOConfig, DDPOTrainer
from trl.trainer.utils import PerPromptStatTracker

logger = get_logger(__name__)


MODEL_CARD_TEMPLATE = """---
license: apache-2.0
tags:
- trl
- diffusion_dpo
- diffusers
- reinforcement-learning
- text-to-image
- stable-diffusion
---

# {model_name}

This is a diffusion model that has been fine-tuned with reinforcement learning to
 guide the model outputs according to a value, function, or human feedback. The model can be used for image generation conditioned with text.

"""


class DDPGTrainer(DDPOTrainer):

    _tag_names = ["trl", "diffusion_dpo"]

    def __init__(
        self,
        config: DDPOConfig,
        reward_function: Callable[[torch.Tensor, Tuple[str], Tuple[Any]], torch.Tensor],
        prompt_function: Callable[[], Tuple[str, Any]],
        sd_pipeline: DDPOStableDiffusionPipeline,
        ref_pipeline: DDPOStableDiffusionPipeline,
        image_samples_hook: Optional[Callable[[Any, Any, Any], Any]] = None,
        epsilon=0.2,
    ):
        super().__init__(
            config,
            reward_function,
            prompt_function,
            sd_pipeline,
            image_samples_hook=image_samples_hook,
        )

        self.ref_pipeline = ref_pipeline
        self.ref_pipeline.set_progress_bar_config(
            position=1,
            disable=not self.accelerator.is_local_main_process,
            leave=False,
            desc="Timestep",
            dynamic_ncols=True,
        )

        if self.accelerator.mixed_precision == "fp16":
            inference_dtype = torch.float16
        elif self.accelerator.mixed_precision == "bf16":
            inference_dtype = torch.bfloat16
        else:
            inference_dtype = torch.float32

        self.ref_pipeline.vae.to(self.accelerator.device, dtype=inference_dtype)
        self.ref_pipeline.text_encoder.to(
            self.accelerator.device, dtype=inference_dtype
        )
        self.ref_pipeline.unet.to(self.accelerator.device, dtype=inference_dtype)

        # Freeze vae, text_encoder, and reference unet
        self.ref_pipeline.vae.requires_grad_(False)
        self.ref_pipeline.text_encoder.requires_grad_(False)
        self.ref_pipeline.unet.requires_grad_(False)

        self.epsilon = epsilon

    def _generate_samples(self, model, iterations, batch_size):
        """
        Generate samples from the model

        Args:
            iterations (int): Number of iterations to generate samples for
            batch_size (int): Batch size to use for sampling

        Returns:
            samples (List[Dict[str, torch.Tensor]]), prompt_image_pairs (List[List[Any]])
        """
        samples = []
        prompt_image_pairs = []
        model.unet.eval()

        sample_neg_prompt_embeds = self.neg_prompt_embed.repeat(batch_size, 1, 1)

        for _ in range(iterations):
            prompts, prompt_metadata = zip(
                *[self.prompt_fn() for _ in range(batch_size)]
            )

            prompt_ids = model.tokenizer(
                prompts,
                return_tensors="pt",
                padding="max_length",
                truncation=True,
                max_length=model.tokenizer.model_max_length,
            ).input_ids.to(self.accelerator.device)
            prompt_embeds = model.text_encoder(prompt_ids)[0]

            with self.autocast():
                sd_output = model(
                    prompt_embeds=prompt_embeds,
                    negative_prompt_embeds=sample_neg_prompt_embeds,
                    num_inference_steps=self.config.sample_num_steps,
                    guidance_scale=self.config.sample_guidance_scale,
                    eta=self.config.sample_eta,
                    output_type="pt",
                )

                images = sd_output.images
                latents = sd_output.latents
                log_probs = sd_output.log_probs

            latents = torch.stack(latents, dim=1)  # (batch_size, num_steps + 1, ...)
            log_probs = torch.stack(log_probs, dim=1)  # (batch_size, num_steps, 1)
            timesteps = model.scheduler.timesteps.repeat(
                batch_size, 1
            )  # (batch_size, num_steps)

            samples.append(
                {
                    "prompt_ids": prompt_ids,
                    "prompt_embeds": prompt_embeds,
                    "timesteps": timesteps,
                    "latents": latents[
                        :, :-1
                    ],  # each entry is the latent before timestep t
                    "next_latents": latents[
                        :, 1:
                    ],  # each entry is the latent after timestep t
                    "log_probs": log_probs,
                    "negative_prompt_embeds": sample_neg_prompt_embeds,
                }
            )
            prompt_image_pairs.append([images, prompts, prompt_metadata])

        return samples, prompt_image_pairs

    def step(self, epoch: int, global_step: int):
        """
        Perform a single step of training.

        Args:
            epoch (int): The current epoch.
            global_step (int): The current global step.

        Side Effects:
            - Model weights are updated
            - Logs the statistics to the accelerator trackers.
            - If `self.image_samples_callback` is not None, it will be called with the prompt_image_pairs, global_step, and the accelerator tracker.

        Returns:
            global_step (int): The updated global step.

        """
        # do an if statement with probability epsilon

        if torch.rand(1) < self.epsilon:
            model = self.ref_pipeline
        else:
            model = self.sd_pipeline

        samples, prompt_image_data = self._generate_samples(
            model=model,
            iterations=self.config.sample_num_batches_per_epoch,
            batch_size=self.config.sample_batch_size,
        )

        # collate samples into dict where each entry has shape (num_batches_per_epoch * sample.batch_size, ...)
        samples = {k: torch.cat([s[k] for s in samples]) for k in samples[0].keys()}
        rewards, rewards_metadata = self.compute_rewards(
            prompt_image_data, is_async=self.config.async_reward_computation
        )

        for i, image_data in enumerate(prompt_image_data):
            image_data.extend([rewards[i], rewards_metadata[i]])

        if self.image_samples_callback is not None:
            self.image_samples_callback(
                prompt_image_data, global_step, self.accelerator.trackers[0]
            )

        rewards = torch.cat(rewards)
        rewards = self.accelerator.gather(rewards).cpu().numpy()

        self.accelerator.log(
            {
                "reward": rewards,
                "epoch": epoch,
                "reward_mean": rewards.mean(),
                "reward_std": rewards.std(),
            },
            step=global_step,
        )

        if self.config.per_prompt_stat_tracking:
            # gather the prompts across processes
            prompt_ids = self.accelerator.gather(samples["prompt_ids"]).cpu().numpy()
            prompts = self.sd_pipeline.tokenizer.batch_decode(
                prompt_ids, skip_special_tokens=True
            )
            advantages = self.stat_tracker.update(prompts, rewards)
        else:
            advantages = (rewards - rewards.mean()) / (rewards.std() + 1e-8)

        # ungather advantages;  keep the entries corresponding to the samples on this process
        samples["advantages"] = (
            torch.as_tensor(advantages)
            .reshape(self.accelerator.num_processes, -1)[self.accelerator.process_index]
            .to(self.accelerator.device)
        )

        del samples["prompt_ids"]

        total_batch_size, num_timesteps = samples["timesteps"].shape

        for inner_epoch in range(self.config.train_num_inner_epochs):
            # shuffle samples along batch dimension
            perm = torch.randperm(total_batch_size, device=self.accelerator.device)
            samples = {k: v[perm] for k, v in samples.items()}

            # shuffle along time dimension independently for each sample
            # still trying to understand the code below
            perms = torch.stack(
                [
                    torch.randperm(num_timesteps, device=self.accelerator.device)
                    for _ in range(total_batch_size)
                ]
            )

            for key in ["timesteps", "latents", "next_latents", "log_probs"]:
                samples[key] = samples[key][
                    torch.arange(total_batch_size, device=self.accelerator.device)[
                        :, None
                    ],
                    perms,
                ]

            original_keys = samples.keys()
            original_values = samples.values()
            # rebatch them as user defined train_batch_size is different from sample_batch_size
            reshaped_values = [
                v.reshape(-1, self.config.train_batch_size, *v.shape[1:])
                for v in original_values
            ]

            # Transpose the list of original values
            transposed_values = zip(*reshaped_values)
            # Create new dictionaries for each row of transposed values
            samples_batched = [
                dict(zip(original_keys, row_values)) for row_values in transposed_values
            ]

            self.sd_pipeline.unet.train()
            global_step = self._train_batched_samples(
                inner_epoch, epoch, global_step, samples_batched
            )
            # ensure optimization step at the end of the inner epoch
            if not self.accelerator.sync_gradients:
                raise ValueError(
                    "Optimization step should have been performed by this point. Please check calculated gradient accumulation settings."
                )

        if (
            epoch != 0
            and epoch % self.config.save_freq == 0
            and self.accelerator.is_main_process
        ):
            self.accelerator.save_state()

        return global_step
