"""Abstract benchmarking dataset class."""

import itertools as it
import logging
import random
import warnings
from abc import ABC, abstractmethod
from collections import defaultdict
from functools import partial
from typing import Any, Callable

import evaluate
import Levenshtein
import numpy as np
import pandas as pd
import torch
from datasets.arrow_dataset import Dataset
from datasets.dataset_dict import DatasetDict
from datasets.load import load_dataset
from huggingface_hub.utils._errors import HfHubHTTPError
from torch.utils.data import DataLoader
from tqdm.auto import tqdm
from transformers import GenerationConfig, PretrainedConfig, StoppingCriteria
from transformers.modeling_utils import ModelOutput, PreTrainedModel
from transformers.trainer import Trainer
from transformers.trainer_callback import (
    EarlyStoppingCallback,
    PrinterCallback,
    ProgressCallback,
    TrainerCallback,
)
from transformers.trainer_utils import IntervalStrategy
from transformers.training_args import OptimizerNames, TrainingArguments

from .callbacks import NeverLeaveProgressCallback
from .config import BenchmarkConfig, DatasetConfig, ModelConfig
from .dataset_tasks import SPEED
from .exceptions import InvalidBenchmark
from .model_config import get_model_config
from .model_loading import load_model
from .model_setups import GenerativeModel, Tokenizer
from .openai_models import OpenAIModel
from .scores import log_scores
from .speed_benchmark import benchmark_speed
from .types import SCORE_DICT
from .utils import (
    block_terminal_output,
    clear_memory,
    enforce_reproducibility,
    handle_error,
    model_is_generative,
)

# Set up logger
logger = logging.getLogger(__name__)


class BenchmarkDataset(ABC):
    """Abstract benchmarking dataset class.

    Args:
        dataset_config (DatasetConfig):
            The configuration of the dataset.
        benchmark_config (BenchmarkConfig):
            The configuration of the benchmark.

    Attributes:
        dataset_config (DatasetConfig):
            The configuration of the dataset.
        benchmark_config (BenchmarkConfig):
            The configuration of the benchmark.
    """

    def __init__(
        self, dataset_config: DatasetConfig, benchmark_config: BenchmarkConfig
    ) -> None:
        """Initialise the dataset.

        Args:
            dataset_config (DatasetConfig):
                The configuration for the dataset.
            benchmark_config (BenchmarkConfig):
                The configuration for the benchmark.
        """
        self.dataset_config = dataset_config
        self.benchmark_config = benchmark_config
        self._metrics = {
            metric_cfg.name: evaluate.load(
                path=metric_cfg.huggingface_id,
                cache_dir=self.benchmark_config.cache_dir,
            )
            if metric_cfg.huggingface_id != ""
            else None
            for metric_cfg in dataset_config.task.metrics
        }

        # Set logging level based on verbosity
        logging_level = logging.DEBUG if self.benchmark_config.verbose else logging.INFO
        logger.setLevel(logging_level)

    def benchmark(self, model_id: str) -> tuple[SCORE_DICT, dict[str, int]]:
        """Benchmark a model.

        Args:
            model_id (str):
                The full Hugging Face Hub path to the pretrained transformer model. The
                specific model version to use can be added after the suffix '@':
                "model_id@v1.0.0". It can be a branch name, a tag name, or a commit id,
                and defaults to the latest version if not specified.

        Returns:
            pair of dicts:
                A pair (scores, metadata_dict), with `scores` being a dictionary
                containing the scores, and `metadata_dict` being a dictionary
                containing various model metadata, such as the number of model
                parameters, the model's maximum sequence length and the size of the
                model's vocabulary. The keys in `score_dict` are 'raw' and 'total',
                with all the raw scores in the first dictionary and the aggregated
                scores in the second.

        Raises:
            RuntimeError:
                If the extracted framework is not recognized.
        """
        model_config = get_model_config(
            model_id=model_id, benchmark_config=self.benchmark_config
        )

        rng = enforce_reproducibility(framework=model_config.framework)

        tokenizer, model = load_model(
            model_config=model_config,
            dataset_config=self.dataset_config,
            benchmark_config=self.benchmark_config,
        )

        # This happens when a local model is used, as we cannot fetch the model metadata
        if model_config.task == "unknown":
            if model_is_generative(model=model):
                model_config.task = "text-generation"
            else:
                model_config.task = "fill-mask"

        metadata_dict = self._get_metadata(model=model, tokenizer=tokenizer)

        # Set variable with number of iterations
        num_iter = 10 if not self.benchmark_config.testing else 5

        if self.dataset_config.task.name != SPEED:
            train, val, tests = self._load_data(num_iter=num_iter, rng=rng)
            prepared_train, prepared_val, prepared_tests = self._load_prepared_data(
                train=train,
                val=val,
                tests=tests,
                model_config=model_config,
                hf_model_config=model.config,
                tokenizer=tokenizer,
            )

        # Set up progress bar
        itr = tqdm(
            iterable=range(num_iter),
            desc="Benchmarking",
            disable=not self.benchmark_config.progress_bar,
        )

        if self.dataset_config.task.name == SPEED:
            scores = benchmark_speed(
                itr=itr,
                tokenizer=tokenizer,
                model=model,
                model_config=model_config,
                dataset_config=self.dataset_config,
                benchmark_config=self.benchmark_config,
            )
        elif model_is_generative(model=model):
            scores = self._generate(
                itr=itr,
                train=train,
                val=val,
                tests=tests,
                prepared_train=prepared_train,
                prepared_val=prepared_val,
                prepared_tests=prepared_tests,
                model=model,
                tokenizer=tokenizer,
            )
        else:
            scores = self._finetune(
                itr=itr,
                train=train,
                val=val,
                tests=tests,
                prepared_train=prepared_train,
                prepared_val=prepared_val,
                prepared_tests=prepared_tests,
                model=model,
                tokenizer=tokenizer,
                model_config=model_config,
            )

        all_scores = log_scores(
            dataset_name=self.dataset_config.pretty_name,
            metric_configs=self.dataset_config.task.metrics,
            scores=scores,
            model_id=model_config.model_id,
        )

        return all_scores, metadata_dict

    def _finetune(
        self,
        itr: tqdm,
        train: Dataset,
        val: Dataset,
        tests: list[Dataset],
        prepared_train: Dataset,
        prepared_val: Dataset,
        prepared_tests: list[Dataset],
        model: PreTrainedModel,
        tokenizer: Tokenizer,
        model_config: ModelConfig,
    ) -> dict[str, list[dict[str, float]]]:
        """Evaluate a model on a dataset through finetuning.

        Args:
            itr (tqdm.tqdm):
                The progress bar iterator.
            train (Dataset):
                The training dataset.
            val (Dataset):
                The validation dataset.
            tests (list[Dataset]):
                The bootstrapped test datasets.
            prepared_train (Dataset):
                The prepared training dataset.
            prepared_val (Dataset):
                The prepared validation dataset.
            prepared_tests (list[Dataset]):
                The prepared bootstrapped test datasets.
            model (PreTrainedModel):
                The model to evaluate.
            tokenizer (Tokenizer):
                The tokenizer to use.
            model_config (ModelConfig):
                The configuration of the model.

        Returns:
            dict[str, list[dict[str, float]]]:
                A dictionary containing the scores, with keys "test" and maybe "train",
                with values being lists of dicts containing the scores for each metric
                for each iteration.
        """
        scores: dict[str, list[dict[str, float]]] = defaultdict(list)

        bs: int = self.benchmark_config.batch_size
        ga: int = 32 // bs
        for idx in itr:
            # Set variable that tracks whether we need to initialize new models in
            # the `finetune_single_iteration` call
            model_already_initialized = idx == 0

            # Clear memory after first iteration
            if not model_already_initialized:
                try:
                    del model
                except UnboundLocalError:
                    pass
                try:
                    del tokenizer
                except UnboundLocalError:
                    pass
                clear_memory()

            while True:
                test = tests[idx]
                prepared_test = prepared_tests[idx]
                assert isinstance(test, Dataset)
                assert isinstance(prepared_test, Dataset)

                # Re-block terminal output, as it gets unblocked by the
                # `transformers` package before training
                block_terminal_output()

                training_args = self._get_training_args(iteration_idx=idx)

                # Set the correct batch size and gradient accumulation
                training_args.per_device_train_batch_size = bs
                training_args.per_device_eval_batch_size = bs
                training_args.gradient_accumulation_steps = ga

                itr_scores = self._finetune_single_iteration(
                    iteration_idx=idx,
                    model_config=model_config,
                    train=train,
                    prepared_train=prepared_train,
                    prepared_val=prepared_val,
                    test=test,
                    prepared_test=prepared_test,
                    training_args=training_args,
                    tokenizer=tokenizer if model_already_initialized else None,
                    model=model if model_already_initialized else None,
                )

                # If the iteration was successful then break the loop
                if isinstance(itr_scores, dict):
                    break

                # Otherwise we encountered an error, so we have to deal with it and
                # try again
                else:
                    bs = training_args.per_device_train_batch_size
                    ga = training_args.gradient_accumulation_steps

                    bs, ga = handle_error(
                        e=itr_scores,
                        per_device_train_batch_size=bs,
                        gradient_accumulation_steps=ga,
                    )

                    # Clear memory, to avoid memory issues
                    try:
                        del model
                    except UnboundLocalError:
                        pass
                    try:
                        del tokenizer
                    except UnboundLocalError:
                        pass
                    clear_memory()
                    model_already_initialized = False

            if "train" in itr_scores:
                logger.debug(f"Train scores for iteration {idx}: {itr_scores['train']}")
                scores["train"].append(itr_scores["train"])
            scores["test"].append(itr_scores["test"])
            logger.debug(f"Test scores for iteration {idx}: {itr_scores['test']}")

        return scores

    def _finetune_single_iteration(
        self,
        iteration_idx: int,
        model_config: ModelConfig,
        train: Dataset,
        test: Dataset,
        prepared_train: Dataset,
        prepared_val: Dataset,
        prepared_test: Dataset,
        training_args: TrainingArguments,
        tokenizer: Tokenizer | None = None,
        model: PreTrainedModel | GenerativeModel | None = None,
    ) -> dict[str, dict[str, float]] | Exception:
        """Run a single iteration of a benchmark.

        Args:
            iteration_idx (int):
                The index of the iteration.
            model_config (ModelConfig):
                The model configuration.
            train (Dataset):
                The original training dataset.
            test (Dataset):
                The original test dataset.
            prepared_train (Dataset):
                The prepared training dataset.
            prepared_val (Dataset):
                The prepared validation dataset.
            prepared_test (Dataset):
                The prepared test dataset.
            training_args (TrainingArguments):
                The training arguments.
            tokenizer (Tokenizer or None, optional):
                The tokenizer to use in the benchmark. If None then a new tokenizer
                will be loaded. Defaults to None.
            model (PreTrainedModel, GenerativeModel or None, optional):
                The model to use in the benchmark. If None then a new model will be
                loaded. Defaults to None.

        Returns:
            dict or Exception:
                A dictionary containing the scores for the current iteration, with keys
                `train` and `test`. If an exception is raised, then the exception is
                returned.
        """
        scores: dict[str, dict[str, float]] = dict()
        try:
            # Set random seeds to enforce reproducibility of the randomly initialised
            # weights
            seed = 4242 + iteration_idx
            enforce_reproducibility(framework=model_config.framework, seed=seed)

            # Reinitialise a new model
            if tokenizer is None or model is None:
                tokenizer, model = load_model(
                    model_config=model_config,
                    dataset_config=self.dataset_config,
                    benchmark_config=self.benchmark_config,
                )

            # Initialise compute_metrics function
            compute_metrics = partial(
                self._compute_metrics, id2label=self.dataset_config.id2label
            )

            # Initialise early stopping callback
            early_stopping = EarlyStoppingCallback(early_stopping_patience=2)

            # Initialise trainer
            trainer = self._get_trainer(
                model=model,
                args=training_args,
                train_dataset=prepared_train,
                eval_dataset=prepared_val,
                tokenizer=tokenizer,
                compute_metrics=compute_metrics,
                callbacks=[early_stopping],
            )

            # Remove trainer logging if not in verbose mode
            if not self.benchmark_config.verbose:
                trainer.log = lambda logs: None

            # Re-block terminal output, as it gets unblocked by the `transformers`
            # package before training
            block_terminal_output()

            # Remove the callback which prints the scores after each evaluation
            if not self.benchmark_config.verbose:
                trainer.remove_callback(PrinterCallback)

            # Remove the progress bar callback
            trainer.remove_callback(ProgressCallback)

            # Add the custom progress callback if `progress_bar` is True
            if self.benchmark_config.progress_bar:
                trainer.add_callback(NeverLeaveProgressCallback)

            # Finetune the model
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", category=UserWarning)
                trainer.train()

            # Log training scores and save the state
            if self.benchmark_config.evaluate_train:
                train_scores = self._evaluate_dataset(
                    dataset=train,
                    prepared_dataset=prepared_train,
                    metric_key_prefix="train",
                    trainer=trainer,
                )
                scores["train"] = train_scores

            # Log test scores
            test_scores = self._evaluate_dataset(
                dataset=test,
                prepared_dataset=prepared_test,
                metric_key_prefix="test",
                trainer=trainer,
            )
            scores["test"] = test_scores

            # Return the scores
            return scores

        except (RuntimeError, ValueError, IndexError) as e:
            try:
                del model
            except UnboundLocalError:
                pass
            try:
                del tokenizer
            except UnboundLocalError:
                pass
            clear_memory()
            return e

    def _generate(
        self,
        itr: tqdm,
        train: Dataset,
        val: Dataset,
        tests: list[Dataset],
        prepared_train: Dataset,
        prepared_val: Dataset,
        prepared_tests: list[Dataset],
        model: GenerativeModel,
        tokenizer: Tokenizer,
    ) -> dict[str, list[dict[str, float]]]:
        """Evaluate a model on a dataset through generation.

        Args:
            itr (tqdm.tqdm):
                The progress bar iterator.
            train (Dataset):
                The training dataset.
            val (Dataset):
                The validation dataset.
            tests (list[Dataset]):
                The bootstrapped test datasets.
            prepared_train (Dataset):
                The prepared training dataset.
            prepared_val (Dataset):
                The prepared validation dataset.
            prepared_tests (list[Dataset]):
                The prepared bootstrapped test datasets.
            num_iter (int):
                The number of iterations to run.
            rng (np.random.Generator):
                The random number generator.
            model (GenerativeModel):
                The model to evaluate.
            tokenizer (Tokenizer):
                The tokenizer to use for the model. If `None` then the model's
                tokenizer will be used.
            model_config (ModelConfig):
                The configuration of the model.

        Returns:
            dict[str, list[dict[str, float]]]:
                A dictionary containing the scores, with keys "test" and maybe "train",
                with values being lists of dicts containing the scores for each metric
                for each iteration.
        """
        scores: dict[str, list[dict[str, float]]] = defaultdict(list)

        for idx in itr:
            prepared_test = prepared_tests[idx]
            assert isinstance(prepared_test, Dataset)

            test_scores = self._generate_single_iteration(
                prepared_dataset=prepared_test,
                model=model,
                tokenizer=tokenizer,
            )
            logger.debug(f"Test scores for iteration {idx}: {test_scores}")
            scores["test"].append(test_scores)

            if self.benchmark_config.evaluate_train:
                train_scores = self._generate_single_iteration(
                    prepared_dataset=prepared_train,
                    model=model,
                    tokenizer=tokenizer,
                )
                logger.debug(f"Train scores for iteration {idx}: {train_scores}")
                scores["train"].append(train_scores)

        return scores

    def _generate_single_iteration(
        self,
        prepared_dataset: Dataset,
        model: GenerativeModel,
        tokenizer: Tokenizer,
    ) -> dict[str, float]:
        """Evaluate a model on a dataset in a single iteration through generation.

        Args:
            prepared_dataset (Dataset):
                The dataset to evaluate on.
            model (GenerativeModel):
                The model to evaluate.
            tokenizer (Tokenizer):
                The tokenizer to use for the model.

        Returns:
            list[dict[str, float]]:
                A list of dictionaries containing the scores for each metric.
        """
        # Tokens used in generation to know when generation is finished
        stopping_criteria = self._get_generation_stopping_criteria(
            tokenizer=tokenizer, model=model
        )

        generation_config = GenerationConfig(
            max_new_tokens=self.dataset_config.max_generated_tokens,
            temperature=0.0,
            do_sample=False,
            stopping_criteria=stopping_criteria,
            output_scores=True,
            return_dict_in_generate=True,
            bos_token_id=tokenizer.bos_token_id,
            eos_token_id=tokenizer.eos_token_id,
            pad_token_id=tokenizer.pad_token_id,
        )

        # Find the largest batch size that fits in memory
        max_seq_len_in_dataset = max(map(len, prepared_dataset["input_ids"]))
        batch_sizes: list[int] = [
            self.benchmark_config.batch_size // (2**n)
            for n in range(1 + np.log2(self.benchmark_config.batch_size).astype(int))
        ]
        batch_size = batch_sizes[0]
        for batch_size in batch_sizes:
            dummy_inputs = torch.full(
                size=(batch_size, max_seq_len_in_dataset),
                fill_value=tokenizer.pad_token_id,
                device=model.device,
                dtype=torch.long,
            )
            try:
                model.generate(dummy_inputs, generation_config=generation_config)
                break
            except Exception as e:
                oom_error = [
                    "CUDA out of memory",
                    "CUDA error",
                    "MPS backend out of memory",
                    "Too many parallel completions requested.",  # OpenAI specific
                ]
                if all(error not in str(e) for error in oom_error):
                    raise InvalidBenchmark(str(e))
                torch.cuda.empty_cache()
                continue
        else:
            raise InvalidBenchmark("GPU out of memory, even with a batch size of 1!")
        self.benchmark_config.batch_size = batch_size

        # Sort the dataset by the length of the text, to minimise the amount of padding
        # that needs to be added, speeding up generation
        text_column = "text" if "text" in prepared_dataset.column_names else "doc"
        prepared_dataset = prepared_dataset.add_column(
            name="length", column=[len(x) for x in prepared_dataset[text_column]]
        )
        prepared_dataset = prepared_dataset.sort("length", reverse=True)

        # Enable batching by building a dataloader. The dataloader cannot deal with
        # text columns, so we create a copy of the dataset without these
        torch_dataset = prepared_dataset.with_format("torch").remove_columns(
            [
                column
                for column in prepared_dataset.column_names
                if column != "input_ids"
            ]
        )

        all_preds: list[str] = list()

        dataloader = DataLoader(
            dataset=torch_dataset,
            batch_size=batch_size,
            shuffle=False,
            num_workers=4,
            collate_fn=self._load_data_collator(tokenizer=tokenizer, model=model),
        )

        for batch in tqdm(dataloader, leave=False):
            # Generate the completions of the documents in the batch
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", category=UserWarning)
                inputs = batch["input_ids"].to(model.device)
                with torch.no_grad():
                    model_output: ModelOutput = model.generate(
                        inputs=inputs, generation_config=generation_config
                    )

            # Extract the predicted labels from the model output
            if self.dataset_config.task.supertask == "sequence-classification":
                if "scores" in model_output:
                    predicted_labels = self._get_closest_logprobs_labels(
                        generation_logprobs=model_output["scores"],
                        tokenizer=tokenizer,
                    )
                else:
                    predicted_labels = self._get_closest_word_edit_labels(
                        generated_sequences=model_output["sequences"],
                        tokenizer=tokenizer,
                    )

            all_preds.extend(predicted_labels)

        true_labels = [
            self.dataset_config.prompt_label_mapping[lbl]
            for lbl in prepared_dataset["label"]
        ]

        itr_scores = self._compute_metrics(
            model_outputs_and_labels=(all_preds, true_labels),
            id2label=self.dataset_config.id2label,
        )

        return itr_scores

    def _get_closest_logprobs_labels(
        self, generation_logprobs: tuple[torch.Tensor], tokenizer: Tokenizer
    ) -> list[str]:
        """Get the labels with the highest predicted logprob value.

        In case a candidate label is split into multiple tokens, we only use the first
        token to compute the logprob value. E.g., if the candidate label "positive" is
        tokenised as ["pos", "itive"], we only use the logprob value of "pos" to
        represent the logprob value of the entire label.

        Args:
            generation_logprobs (tuple[torch.Tensor]):
                The logprobs of the generated tokens.
            tokenizer (Tokenizer):
                The tokenizer used to generate the tokens.

        Returns:
            list[str]:
                The predicted labels.
        """
        candidate_labels = [
            self.dataset_config.prompt_label_mapping[lbl]
            for lbl in self.dataset_config.id2label
        ]

        # Shape: [batch_size, num_generated_tokens, vocab_size]
        all_logprobs = torch.stack(generation_logprobs, dim=1)

        # Shape: [batch_size, num_candidate_labels]
        pred_logprobs = torch.empty(
            all_logprobs.shape[0], len(candidate_labels), device=all_logprobs.device
        )

        for idx, candidate_label in enumerate(candidate_labels):
            # We only use the first token to represent the logprob value of the entire
            # label.
            candidate_label_ids: list[list[int]] = tokenizer(
                [candidate_label.lower()], add_special_tokens=False
            )["input_ids"]
            candidate_label_id: int = candidate_label_ids[0][0]
            pred_logprobs[:, idx] = all_logprobs[:, 0, candidate_label_id]

        # Shape: [batch_size,]
        predicted_label_ids = pred_logprobs.argmax(dim=1)

        return [candidate_labels[idx] for idx in predicted_label_ids]

    def _get_closest_word_edit_labels(
        self, generated_sequences: list[list[int]], tokenizer: Tokenizer
    ) -> list[str]:
        """Get the labels with the smallest edit distance to the predicted labels.

        Args:
            generated_sequences (list of list of int):
                The generated sequences from the model. The outer-most list is the
                batch dimension, the inner-most list is the sequence dimension,
                consisting of token IDs.
            tokenizer (Tokenizer):
                The tokenizer used to generate the tokens.

        Returns:
            list of str:
                The candidate labels with the smallest edit distance to the predicted
                labels.
        """
        raw_predictions = self._extract_raw_predictions(
            generated_sequences=generated_sequences, tokenizer=tokenizer
        )

        candidate_labels = self.dataset_config.id2label
        new_predicted_labels: list[str] = list()
        for predicted_label in raw_predictions:
            edit_distances = [
                Levenshtein.distance(
                    s1=predicted_label.lower(), s2=candidate_label.lower()
                )
                for candidate_label in candidate_labels
            ]
            closest_label = candidate_labels[np.argmin(edit_distances).item()]
            new_predicted_labels.append(closest_label)
        return new_predicted_labels

    def _extract_raw_predictions(
        self, generated_sequences: list[list[int]], tokenizer: Tokenizer
    ) -> list[str]:
        """Get the labels with the smallest edit distance to the predicted labels.

        Args:
            generated_sequences (list of list of int):
                The generated sequences from the model. The outer-most list is the
                batch dimension, the inner-most list is the sequence dimension,
                consisting of token IDs.
            tokenizer (Tokenizer):
                The tokenizer used to generate the tokens.

        Returns:
            list of str:
                The candidate labels with the smallest edit distance to the predicted
                labels.
        """

        completion_ids_lists = [
            [
                token_id
                for token_id in completion_ids
                if token_id not in [tokenizer.bos_token_id, tokenizer.eos_token_id]
            ]
            for completion_ids in generated_sequences
        ]

        # For some models the generated tokens also includes the input tokens, so
        # we need to deal with both cases when extracting the predicted labels
        try:
            pred_idx = self.dataset_config.num_few_shot_examples + 1
            if self.dataset_config.prompt_instruction_infix:
                pred_idx += 1
            return [
                tokenizer.decode(completion_ids_list)
                .split("\n\n")[pred_idx]
                .split("\n")[-1]
                .split(":")[-1]
                .strip()
                for completion_ids_list in completion_ids_lists
            ]
        except IndexError:
            return [
                tokenizer.decode(completion_ids_list).strip()
                for completion_ids_list in completion_ids_lists
            ]

    def _get_generation_stopping_criteria(
        self,
        tokenizer: Tokenizer,
        model: GenerativeModel,
    ) -> list[StoppingCriteria]:
        """Get the stopping criteria for generation.

        Args:
            tokenizer (Tokenizer):
                The tokenizer used to tokenize the stop words.
            model (GenerativeModel):
                The generative model, which we use to ensure the tensors are on the
                same device, and also determine whether stop words are needed, based on
                the model type.

        Returns:
            list[torch.Tensor]:
                A list of tensors containing the stop words to use for generation.
        """
        if isinstance(model, OpenAIModel):
            return list()

        stop_word_ids: list[torch.Tensor] = list()

        double_newline_ids: torch.Tensor = (
            tokenizer(
                text=["\n\n"],
                add_special_tokens=False,
                return_tensors="pt",
            )
            .input_ids[0]
            .to(model.device)
        )
        single_newline_ids: torch.Tensor = (
            tokenizer(
                text=["\n"],
                add_special_tokens=False,
                return_tensors="pt",
            )
            .input_ids[0]
            .to(model.device)
        )
        bos_token_ids: torch.Tensor = (
            tokenizer(
                text=[tokenizer.bos_token],
                add_special_tokens=False,
                return_tensors="pt",
            )
            .input_ids[0]
            .to(model.device)
        )
        eos_token_ids: torch.Tensor = (
            tokenizer(
                text=[tokenizer.eos_token],
                add_special_tokens=False,
                return_tensors="pt",
            )
            .input_ids[0]
            .to(model.device)
        )

        double_newline_ids = double_newline_ids[
            [tokenizer.decode(tok) != "" for tok in double_newline_ids]
        ]
        single_newline_ids = single_newline_ids[
            [tokenizer.decode(tok) != "" for tok in single_newline_ids]
        ]
        bos_token_ids = bos_token_ids[
            [tokenizer.decode(tok) != "" for tok in bos_token_ids]
        ]
        eos_token_ids = eos_token_ids[
            [tokenizer.decode(tok) != "" for tok in eos_token_ids]
        ]

        two_single_newline_ids = torch.cat(
            [single_newline_ids, single_newline_ids], dim=0
        )

        stop_word_ids = [
            double_newline_ids,
            two_single_newline_ids,
            bos_token_ids,
            eos_token_ids,
        ]

        return [StopWordCriteria(stop_word_ids=stop_word_ids)]

    def _get_metadata(
        self,
        model: PreTrainedModel | GenerativeModel,
        tokenizer: Tokenizer,
    ) -> dict[str, int]:
        """Get metadata about the model.

        Args:
            model (PreTrainedModel or GenerativeModel):
                The model to get metadata about.
            tokenizer (Tokenizer):
                The tokenizer to get metadata about.

        Returns:
            dict[str, int]:
                A dictionary containing metadata about the model, with the keys being
                the metadata names and the values being the metadata values.
        """
        if hasattr(model.config, "num_params"):
            num_params = model.config.num_params
        elif isinstance(model, PreTrainedModel):
            num_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
        else:
            num_params = -1

        if hasattr(model.config, "model_max_length"):
            max_seq_length = getattr(model.config, "model_max_length")
        elif hasattr(tokenizer, "model_max_length"):
            max_seq_length = getattr(tokenizer, "model_max_length")
        else:
            max_seq_length = -1

        if hasattr(model.config, "vocab_size"):
            vocab_size = getattr(model.config, "vocab_size")
        elif hasattr(tokenizer, "vocab_size"):
            vocab_size = getattr(tokenizer, "vocab_size")
        else:
            vocab_size = -1

        # Store the metadata in a dictionary
        metadata_dict = dict(
            num_model_parameters=num_params,
            max_sequence_length=max_seq_length,
            vocabulary_size=vocab_size,
        )

        # Log the metadata
        logger.info(
            f"The model has {num_params:,} parameters, a vocabulary size of "
            f"{vocab_size:,} and a maximum sequence length of {max_seq_length:,}."
        )

        return metadata_dict

    def _get_training_args(self, iteration_idx: int) -> TrainingArguments:
        """Get the training arguments for the current iteration.

        Args:
            iteration_idx (int):
                The index of the current iteration. This is only used to generate a
                unique random seed for the current iteration.

        Returns:
            TrainingArguments:
                The training arguments for the current iteration.
        """
        # Set the logging strategy
        if self.benchmark_config.verbose:
            logging_strategy = IntervalStrategy.STEPS
        else:
            logging_strategy = IntervalStrategy.NO

        # Set seed variable
        seed = 4242 + iteration_idx

        # Initialise training arguments
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=UserWarning)
            training_args = TrainingArguments(
                output_dir=self.benchmark_config.cache_dir,
                evaluation_strategy=IntervalStrategy.STEPS,
                logging_strategy=logging_strategy,
                save_strategy=IntervalStrategy.STEPS,
                eval_steps=30,
                logging_steps=30,
                save_steps=30,
                max_steps=10_000 if not self.benchmark_config.testing else 10,
                report_to=[],
                save_total_limit=1,
                per_device_train_batch_size=self.benchmark_config.batch_size,
                per_device_eval_batch_size=self.benchmark_config.batch_size,
                learning_rate=2e-5,
                warmup_ratio=0.01,
                gradient_accumulation_steps=1,
                load_best_model_at_end=True,
                optim=OptimizerNames.ADAMW_TORCH,
                seed=seed,
                use_mps_device=torch.backends.mps.is_available(),
                fp16=False,
            )

        # Manually set `disable_tqdm` to `False` if `progress_bar` is `True`
        if self.benchmark_config.progress_bar:
            training_args.disable_tqdm = False

        return training_args

    def _load_data(
        self,
        num_iter: int,
        rng: np.random.Generator,
    ) -> tuple[Dataset, Dataset, list[Dataset]]:
        """Load the raw bootstrapped datasets.

        Args:
            num_iter (int):
                The number of iterations to run.
            rng (np.random.Generator):
                The random number generator to use.

        Returns:
            tuple[Dataset, Dataset, list[Dataset]]:
                A tuple containing the training, validation and test datasets.
        """
        # Download dataset from the HF Hub
        try:
            dataset_dict = load_dataset(
                path=self.dataset_config.huggingface_id,
                use_auth_token=self.benchmark_config.use_auth_token,
                cache_dir=self.benchmark_config.cache_dir,
            )
        except HfHubHTTPError:
            raise InvalidBenchmark("The Hugging Face Hub seems to be down.")

        # If the dataset turns out not to be a DatasetDict, then we raise an error
        if not isinstance(dataset_dict, DatasetDict):
            raise InvalidBenchmark(
                f"Expected `dataset_dict` to be a `DatasetDict`, but got "
                f"{type(dataset_dict)}."
            )

        # Remove all other keys than 'train', 'val' and 'test'
        dataset_dict = DatasetDict(
            {key: dataset_dict[key] for key in ["train", "val", "test"]}
        )

        # Process the datasets
        dataset_dict = self._process_data(dataset_dict)

        # Extract the dataset splits
        train = dataset_dict["train"]
        val = dataset_dict["val"]
        test = dataset_dict["test"]

        # TEMP
        test = val

        # Remove empty examples from the datasets
        for text_feature in ["tokens", "doc", "text"]:
            if text_feature in train.features:
                train = train.filter(lambda x: len(x[text_feature]) > 0)
                val = val.filter(lambda x: len(x[text_feature]) > 0)
                test = test.filter(lambda x: len(x[text_feature]) > 0)

        # If we are testing then truncate the test set
        if self.benchmark_config.testing:
            test = test.select(range(128))

        # Bootstrap the test set
        test_bidxs = rng.integers(0, len(test), size=(num_iter, len(test)))
        tests = [test.select(test_bidxs[idx]) for idx in range(test_bidxs.shape[0])]

        return train, val, tests

    def _load_prepared_data(
        self,
        train: Dataset,
        val: Dataset,
        tests: list[Dataset],
        model_config: ModelConfig,
        hf_model_config: PretrainedConfig,
        tokenizer: Tokenizer,
    ) -> tuple[Dataset, Dataset, list[Dataset]]:
        """Load the data and prepare it for training.

        Args:
            train (Dataset):
                The raw training dataset.
            val (Dataset):
                The raw validation dataset.
            tests (list[Dataset]):
                The raw bootstrapped test datasets.
            model_config (ModelConfig):
                The model configuration.
            hf_model_config (PretrainedConfig):
                The Hugging Face model configuration.
            tokenizer (Tokenizer):
                The tokenizer.

        Returns:
            tuple[Dataset, Dataset, list[Dataset]]:
                A tuple containing the prepared training, validation and test datasets.
        """
        # Set up the preprocessing parameters
        preprocess_params: dict[str, Any] = dict(
            hf_model_config=hf_model_config,
            model_config=model_config,
            tokenizer=tokenizer,
        )

        # Prepare the train and validation datasets
        try:
            with tqdm(total=12, desc="Preprocessing data splits", leave=False) as pbar:
                prepared_train = self._preprocess_data(
                    train, split="train", **preprocess_params
                )
                pbar.update(1)

                prepared_val = self._preprocess_data(
                    val, split="val", **preprocess_params
                )
                pbar.update(1)

                prepared_tests: list[Dataset] = list()
                for itr_idx, test in enumerate(tests):
                    if model_config.task == "text-generation":
                        itr_seed = 4242 + itr_idx
                        shuffled_train = train.shuffle(seed=itr_seed)
                        num_few_shots = self.dataset_config.num_few_shot_examples

                        supertask = self.dataset_config.task.supertask
                        if supertask == "sequence-classification":
                            labels = it.cycle(self.dataset_config.task.labels)
                            few_shot_examples: list[dict] = list()
                            while len(few_shot_examples) < num_few_shots:
                                label = next(labels)
                                example = shuffled_train.filter(
                                    lambda x: x["label"].lower() == label.lower()
                                ).select(range(1))[0]
                                few_shot_examples.append(example)
                                shuffled_train = shuffled_train.filter(
                                    lambda x: x["text"] != example["text"]
                                )
                        else:
                            examples_df = shuffled_train.select(
                                range(num_few_shots)
                            ).to_pandas()
                            assert isinstance(examples_df, pd.DataFrame)
                            few_shot_examples = examples_df.to_dict("records")

                        random.seed(itr_seed)
                        random.shuffle(few_shot_examples)
                        preprocess_params["few_shot_examples"] = few_shot_examples
                    prepared_tests.append(
                        self._preprocess_data(test, split="test", **preprocess_params)
                    )
                    pbar.update(1)
        except ValueError:
            raise InvalidBenchmark(
                "Preprocessing of the training and validation datasets could not be "
                "done."
            )

        return prepared_train, prepared_val, prepared_tests

    def _extract_few_shot_examples(self, shuffled_train: Dataset) -> list[dict]:
        supertask = self.dataset_config.task.supertask
        num_few_shots = self.dataset_config.num_few_shot_examples
        if supertask == "sequence-classification":
            labels = it.cycle(self.dataset_config.task.labels)
            few_shot_examples: list[dict] = list()
            while len(few_shot_examples) < num_few_shots:
                label = next(labels)
                examples = shuffled_train.filter(
                    lambda x: x["label"].lower() == label.lower()
                ).select(range(1))
                few_shot_examples.append(examples[0])
        else:
            examples_df = shuffled_train.select(range(num_few_shots)).to_pandas()
            assert isinstance(examples_df, pd.DataFrame)
            few_shot_examples = examples_df.to_dict("records")
        return few_shot_examples

    def __call__(self, *args, **kwargs):
        return self.benchmark(*args, **kwargs)

    def _get_trainer(
        self,
        model: PreTrainedModel | GenerativeModel,
        args: TrainingArguments,
        train_dataset: Dataset,
        eval_dataset: Dataset,
        tokenizer: Tokenizer,
        compute_metrics: Callable,
        callbacks: list[TrainerCallback],
    ) -> Trainer:
        """Get a Trainer object.

        Args:
            model (PreTrainedModel or GenerativeModel):
                The model to finetune.
            args (TrainingArguments):
                The training arguments.
            train_dataset (Dataset):
                The training dataset.
            eval_dataset (Dataset):
                The evaluation dataset.
            tokenizer (Tokenizer):
                The tokenizer.
            compute_metrics (Callable):
                The function used to compute the metrics.
            callbacks (list of TrainerCallback):
                The callbacks to use.

        Returns:
            Trainer:
                The Trainer object.
        """
        return Trainer(
            model=model,
            args=args,
            train_dataset=train_dataset,
            eval_dataset=eval_dataset,
            tokenizer=tokenizer,
            compute_metrics=compute_metrics,
            callbacks=callbacks,
            data_collator=self._load_data_collator(tokenizer=tokenizer, model=model),
        )

    def _evaluate_dataset(
        self,
        trainer: Trainer,
        dataset: Dataset,
        prepared_dataset: Dataset,
        metric_key_prefix: str,
    ) -> dict[str, float]:
        """Evaluate a dataset.

        Args:
            trainer (Trainer):
                The trainer.
            dataset (Dataset):
                The original dataset.
            prepared_dataset (Dataset):
                The prepared dataset.
            metric_key_prefix (str):
                The prefix to use for the metric keys.

        Returns:
            dict:
                The scores.
        """
        return trainer.evaluate(
            eval_dataset=prepared_dataset, metric_key_prefix=metric_key_prefix
        )

    def _process_data(self, dataset_dict: DatasetDict) -> DatasetDict:
        """Process the data.

        Args:
            dataset_dict (DatasetDict):
                The dataset dictionary.

        Returns:
            DatasetDict:
                The processed dataset dictionary.
        """
        return dataset_dict

    @abstractmethod
    def _preprocess_data(self, dataset: Dataset, **kwargs) -> Dataset:
        """Preprocess a dataset.

        Args:
            dataset (Hugging Face dataset):
                The dataset to preprocess.
            kwargs:
                Extra keyword arguments containing objects used in preprocessing the
                dataset.

        Returns:
            Hugging Face dataset:
                The preprocessed dataset.
        """
        pass

    @abstractmethod
    def _load_data_collator(
        self,
        tokenizer: Tokenizer | None = None,
        model: PreTrainedModel | GenerativeModel | None = None,
    ):
        """Load the data collator used to prepare samples during finetuning.

        Args:
            tokenizer (Tokenizer or None, optional):
                A pretrained tokenizer. Can be None if the tokenizer is not used in the
                initialisation of the data collator. Defaults to None.
            model (PreTrainedModel or GenerativeModel or None, optional):
                A pretrained model. Can be None if the model is not used in the
                initialisation of the data collator. Defaults to None.

        Returns:
            Hugging Face data collator:
                The data collator.
        """
        pass

    def _compute_metrics(
        self,
        model_outputs_and_labels: tuple[list[int] | list[str], list[int] | list[str]],
        id2label: list[str],
    ) -> dict[str, float]:
        """Compute the metrics needed for evaluation.

        Args:
            model_outputs_and_labels (pair of sequences):
                The first sequence contains the model outputs and the second sequence
                contains the true labels.
            id2label (list or None, optional):
                Conversion of indices to labels. Defaults to None.

        Returns:
            dict:
                A dictionary with the names of the metrics as keys and the metric
                values as values.
        """
        model_outputs, labels = model_outputs_and_labels

        model_output_dtype = np.asarray(model_outputs).dtype
        if model_output_dtype in [np.float16, np.float32, np.float64]:
            predictions = np.asarray(model_outputs).argmax(axis=-1)
        else:
            predictions = model_outputs

        prompt_label_to_label_mapping = {
            prompt_label: label
            for label, prompt_label in self.dataset_config.prompt_label_mapping.items()
        }
        predictions = [
            id2label.index(prompt_label_to_label_mapping[pred.lower()])
            if isinstance(pred, str)
            else pred
            for pred in predictions
        ]

        labels = [
            id2label.index(prompt_label_to_label_mapping[label.lower()])
            if isinstance(label, str)
            else label
            for label in labels
        ]

        results: dict[str, float] = dict()
        for cfg in self.dataset_config.task.metrics:
            metric = self._metrics[cfg.name]
            score_dict: dict[str, float] | None = metric.compute(
                predictions=predictions,
                references=labels,
                **cfg.compute_kwargs,
            )
            if score_dict is not None:
                scores = score_dict[cfg.results_key]
                results[cfg.name] = scores
        return results


class StopWordCriteria(StoppingCriteria):
    def __init__(self, stop_word_ids: list[torch.Tensor]):
        super().__init__()
        self.stop_word_ids = stop_word_ids

    def __call__(self, input_ids: torch.LongTensor, scores: torch.FloatTensor) -> bool:
        for stop_word_tensor in self.stop_word_ids:
            if torch.all(
                (stop_word_tensor == input_ids[0][-len(stop_word_tensor) :])
            ).item():
                return True
        return False
