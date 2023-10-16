import os
os.environ["TOKENIZERS_PARALLELISM"] = "false"
import os.path as p
import gc
import sys

from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    default_data_collator,
    get_linear_schedule_with_warmup,
    set_seed,
)
import torch
import logging
import pandas as pd
from peft import LoraConfig, TaskType, get_peft_model, PeftModel, PeftConfig
from tqdm import tqdm

import util.main_utils as main_utils
from util.train_utils import (
    _collate_fn, _flatten, _find_save_path,
    _load_state, _save_state
)
import dataset.d_argument as DS

if __name__ == '__main__':    
    country = sys.argv[1]
    GPU_NUM = sys.argv[2]
    strategy = 'min'  
    threshold = 3  
    
    
    device = torch.device(f'cuda:{GPU_NUM}' if torch.cuda.is_available() else 'cpu')
    
    model_name = 'llama'
    model_name_or_path = 'decapoda-research/llama-7b-hf'
        
    peft_config = LoraConfig(task_type=TaskType.CAUSAL_LM, inference_mode=False, r=8, lora_alpha=32, lora_dropout=0.1)
    lr = 2e-5
    num_epochs = 5
    batch_size = 4
    seed = 42
    set_seed(seed)
    
    path = f'./logs/argument/{model_name}/TH_{threshold}'
    
    os.makedirs(path, exist_ok=True)

    tokenizer = AutoTokenizer.from_pretrained(model_name_or_path)
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token_id = tokenizer.eos_token_id

    train_pos_df = pd.read_csv(f'./data/country/argument/train/TH_{threshold}/pos/{country}.csv', sep='\t')
    valid_pos_df = pd.read_csv(f'./data/country/argument/valid/TH_{threshold}/pos/{country}.csv', sep='\t')
    train_neg_df = pd.read_csv(f'./data/country/argument/train/TH_{threshold}/neg/{country}.csv', sep='\t')
    valid_neg_df = pd.read_csv(f'./data/country/argument/valid/TH_{threshold}/neg/{country}.csv', sep='\t')

    train_ds = DS.DS_argument(tokenizer, train_pos_df, train_neg_df)
    valid_ds = DS.DS_argument(tokenizer, valid_pos_df, valid_neg_df)
    train_dataloader = torch.utils.data.DataLoader(train_ds, batch_size=batch_size, shuffle=True, collate_fn=_collate_fn, pin_memory=True)
    valid_dataloader = torch.utils.data.DataLoader(valid_ds, batch_size=batch_size, collate_fn=_collate_fn, pin_memory=True)
    
    model = AutoModelForCausalLM.from_pretrained(model_name_or_path)
    model = get_peft_model(model, peft_config)
    model.print_trainable_parameters()

    optimizer = torch.optim.AdamW(model.parameters(), lr=lr)

    lr_scheduler = get_linear_schedule_with_warmup(
        optimizer=optimizer,
        num_warmup_steps=0,
        num_training_steps=(len(train_dataloader) * num_epochs),
    )
    
    model = model.to(device)
    best_loss = float('inf')
    patience_flag = 0
    for epoch in range(num_epochs):
        model.train()
        total_loss = 0
        for step, (input_ids, attention_mask, labels) in enumerate(tqdm(train_dataloader)):
            input_ids, attention_mask, labels = input_ids.to(device), attention_mask.to(device), labels.to(device)
            outputs = model(input_ids=input_ids, attention_mask=attention_mask, labels=labels)
            loss = outputs.loss
            loss.backward()
            total_loss += loss.item()

            optimizer.step()
            lr_scheduler.step()
            optimizer.zero_grad()

        model.eval()
        eval_loss = 0
        for step, (input_ids, attention_mask, labels) in enumerate(tqdm(valid_dataloader)):
            input_ids, attention_mask, labels = input_ids.to(device), attention_mask.to(device), labels.to(device)
            with torch.no_grad():
                outputs = model(input_ids=input_ids, attention_mask=attention_mask, labels=labels)
            loss = outputs.loss
            eval_loss += loss.item()
            
        train_epoch_loss = total_loss / len(train_dataloader)
        eval_epoch_loss = eval_loss / len(valid_dataloader)
        if eval_epoch_loss < best_loss:
            peft_model_id = f"./ckpt/argument/{model_name}/TH_{threshold}/{country}/epoch_{epoch+1}"
            model.save_pretrained(peft_model_id)
            best_loss = eval_epoch_loss
