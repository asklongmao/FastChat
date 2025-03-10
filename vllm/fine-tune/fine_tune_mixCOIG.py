import os
import math
import pathlib
from typing import Optional, Dict
from dataclasses import dataclass, field
import json,re

import torch
from torch.utils.data import Dataset
import transformers
from transformers.training_args import TrainingArguments


@dataclass
class ModelArguments:
    model_name_or_path: Optional[str] = field(default="baichuan-inc/Baichuan2-7B-Base")


@dataclass
class DataArguments:
    data_dir: Optional[str] = field(
        default=None, metadata={"help": "Directory containing training data files."}
    )
    # data_cate: Optional[str] = field(
    #     default=None, metadata={"help": "Path to the directory containing training data files."}
    # )

@dataclass
class TrainingArguments(transformers.TrainingArguments):
    cache_dir: Optional[str] = field(default=None)
    optim: str = field(default="adamw_torch")
    model_max_length: int = field(
        default=512,
        metadata={
            "help": "Maximum sequence length. Sequences will be right padded (and possibly truncated)."
        },
    )
    use_lora: bool = field(default=False)


class SupervisedDataset(Dataset):
    """Dataset for supervised fine-tuning."""

    def __init__(
        self,
        data_dir,
        tokenizer,
        model_max_length,
        user_tokens=[195],
        assistant_tokens=[196],
    ):
        super(SupervisedDataset, self).__init__()
        self.tmp = 0
        self.data = []
        # List all JSON files in the data_dir
        data_files = pathlib.Path(data_dir).glob("*.jsonl")

        # 过滤文件
        exclude_keywords = ["yayi", "firefly", "OL-CC", "COIG"]
        coig_files = []
        other_files = []

        for file in data_files:
            if "COIG" in file.name:
                coig_files.append(file)
            elif not any(keyword in file.name for keyword in exclude_keywords):
                other_files.append(file)

        # 计算每个文件应该读取的行数
        total_lines_needed = 100000
        lines_per_file = total_lines_needed // len(other_files)

        # 读取数据
        for file in other_files:
            with open(file, 'r') as f:
                total_lines = sum(1 for _ in f)
                skip_lines = max(1, total_lines // lines_per_file)
                f.seek(0)  # Reset file pointer to the beginning
                for index, line in enumerate(f):
                    if index % skip_lines == 0:
                        self.data.append(json.loads(line))

        for file in coig_files:
            with open(file, 'r') as f:
                for line in f:
                    self.data.append(json.loads(line))



        print(f"Total number of data entries read: {len(self.data)}")


        # if data_dir:
        #     data_files = pathlib.Path(data_dir).glob("*.jsonl")
        #     for file in data_files:
        #         with open(file, 'r') as f:
        #             for line in f:
        #                 self.data.append(json.loads(line))
        # else:
        #     with open(data_path, 'r') as f:
        #         for line in f:
        #             self.data.append(json.loads(line))

        #self.data = [item for item in self.data if self.is_valid_item(item)]  # 过滤数据
        # self.data = [item for item in self.data]
        # self.data = self.data[:int(0.8*len(self.data))]
        self.tokenizer = tokenizer
        self.model_max_length = model_max_length
        self.user_tokens = user_tokens
        self.assistant_tokens = assistant_tokens
        self.ignore_index = -100
        item = self.preprocessing(self.data[0])
        # print("input:", self.tokenizer.decode(item["input_ids"]))
        labels = []
        for id_ in item["labels"]:
            if id_ == -100:
                continue

            labels.append(id_)
        print("label:", self.tokenizer.decode(labels))

    def is_valid_item(self, item):
        # 你可以在这里加入其他的检查逻辑
        input_data = item.get('input', "")
        if all(char in input_data for char in "ABCD"):
            self.tmp += 1
            return False
        return True
    

    def __len__(self):
        return len(self.data)

    def preprocessing(self, example):
        input_ids = []
        labels = []

        # 使用新的数据格式
        instruction = example["instruction"] + example['input']
        output = example['output']
        # 对instruction进行编码
        instruction_ids = self.tokenizer.encode(instruction)
        output_ids = self.tokenizer.encode(output)

        # 将instruction添加到input_ids
        input_ids += self.user_tokens + instruction_ids
        labels += [self.tokenizer.eos_token_id] + [self.ignore_index] * len(instruction_ids)

        # 将output添加到input_ids
        input_ids += self.assistant_tokens + output_ids
        labels += [self.ignore_index] + output_ids

        input_ids.append(self.tokenizer.eos_token_id)
        labels.append(self.tokenizer.eos_token_id)

        # 填充到模型的最大长度
        input_ids = input_ids[: self.model_max_length]
        labels = labels[: self.model_max_length]
        input_ids += [self.tokenizer.pad_token_id] * (self.model_max_length - len(input_ids))
        labels += [self.ignore_index] * (self.model_max_length - len(labels))

        input_ids = torch.LongTensor(input_ids)
        labels = torch.LongTensor(labels)
        attention_mask = input_ids.ne(self.tokenizer.pad_token_id)
        return {
            "input_ids": input_ids,
            "labels": labels,
            "attention_mask": attention_mask,
        }


    def __getitem__(self, idx) -> Dict[str, torch.Tensor]:
        return self.preprocessing(self.data[idx])


def train():
    parser = transformers.HfArgumentParser(
        (ModelArguments, DataArguments, TrainingArguments)
    )
    model_args, data_args, training_args = parser.parse_args_into_dataclasses()
    model = transformers.AutoModelForCausalLM.from_pretrained(
        model_args.model_name_or_path,
        trust_remote_code=True,
        cache_dir=training_args.cache_dir,
    )
    tokenizer = transformers.AutoTokenizer.from_pretrained(
        model_args.model_name_or_path,
        use_fast=False,
        trust_remote_code=True,
        model_max_length=training_args.model_max_length,
        cache_dir=training_args.cache_dir,
    )
    if training_args.use_lora:
        from peft import LoraConfig, TaskType, get_peft_model

        peft_config = LoraConfig(
            task_type=TaskType.CAUSAL_LM,
            target_modules=["W_pack"],
            inference_mode=False,
            r=1,
            lora_alpha=32,
            lora_dropout=0.1,
        )
        model.enable_input_require_grads()
        model = get_peft_model(model, peft_config)
        model.print_trainable_parameters()

    dataset = SupervisedDataset(
        data_args.data_dir, tokenizer, training_args.model_max_length
    )
    trainer = transformers.Trainer(
        model=model, args=training_args, train_dataset=dataset, tokenizer=tokenizer
    )
    trainer.train()
    trainer.save_state()
    trainer.save_model(output_dir=training_args.output_dir)


if __name__ == "__main__":
    train()
