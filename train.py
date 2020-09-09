# Copyright 2020 Dakewe Biotech Corporation. All Rights Reserved.
# Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
import argparse
import math
import os
import random

import torch.backends.cudnn as cudnn
import torch.nn as nn
import torch.optim as optim
import torch.utils.data
import torch.utils.data.distributed
from torch.cuda import amp

from srcnn_pytorch import DatasetFromFolder
from srcnn_pytorch import SRCNN

parser = argparse.ArgumentParser(description="PyTorch Super Resolution CNN.")
parser.add_argument("--dataroot", type=str, default="./data/DIV2K",
                    help="Path to datasets. (default:`./data/DIV2K`)")
parser.add_argument("-j", "--workers", default=0, type=int, metavar="N",
                    help="Number of data loading workers. (default:0)")
parser.add_argument("--epochs", default=200, type=int, metavar="N",
                    help="Number of total epochs to run. (default:200)")
parser.add_argument("-b", "--batch-size", default=64, type=int,
                    metavar="N",
                    help="mini-batch size (default: 64), this is the total "
                         "batch size of all GPUs on the current node when "
                         "using Data Parallel or Distributed Data Parallel.")
parser.add_argument("--lr", type=float, default=0.0001,
                    help="Learning rate. (default:0.0001)")
parser.add_argument("--scale-factor", type=int, default=4,
                    help="Low to high resolution scaling factor. (default:4).")
parser.add_argument("-p", "--print-freq", default=5, type=int,
                    metavar="N", help="Print frequency. (default:5)")
parser.add_argument("--cuda", action="store_true", help="Enables cuda")
parser.add_argument("--weights", default="",
                    help="Path to weights (to continue training).")
parser.add_argument("--manualSeed", type=int,
                    help="Seed for initializing training. (default:none)")

args = parser.parse_args()
print(args)

try:
    os.makedirs("weights")
except OSError:
    pass

if args.manualSeed is None:
    args.manualSeed = random.randint(1, 10000)
print("Random Seed: ", args.manualSeed)
random.seed(args.manualSeed)
torch.manual_seed(args.manualSeed)

cudnn.benchmark = True

if torch.cuda.is_available() and not args.cuda:
    print("WARNING: You have a CUDA device, "
          "so you should probably run with --cuda")

train_dataset = DatasetFromFolder(f"{args.dataroot}/train",
                                  scale_factor=args.scale_factor)
val_dataset = DatasetFromFolder(f"{args.dataroot}/val",
                                scale_factor=args.scale_factor)

train_dataloader = torch.utils.data.DataLoader(train_dataset,
                                               batch_size=args.batch_size,
                                               shuffle=True, pin_memory=True,
                                               num_workers=int(args.workers))
val_dataloader = torch.utils.data.DataLoader(val_dataset,
                                             batch_size=args.batch_size,
                                             shuffle=False, pin_memory=True,
                                             num_workers=int(args.workers))

device = torch.device("cuda:0" if args.cuda else "cpu")

model = SRCNN().to(device)

if args.weights:
    model.load_state_dict(torch.load(args.weights, map_location=device))
criterion = nn.MSELoss().to(device)
optimizer = optim.Adam(
    # we use Adam instead of SGD like in the paper, because it's faster
    [
        {"params": model.conv1.parameters(), "lr": args.lr},
        {"params": model.conv2.parameters(), "lr": args.lr},
        {"params": model.conv3.parameters(), "lr": args.lr},
    ], lr=args.lr * 0.1,
)

best_psnr = 0.

# Creates a GradScaler once at the beginning of training.
scaler = amp.GradScaler()

for epoch in range(args.epochs):

    # Train
    epoch_loss = 0
    for iteration, batch in enumerate(train_dataloader):
        inputs, target = batch[0].to(device), batch[1].to(device)
        optimizer.zero_grad()

        # Runs the forward pass with autocasting.
        with amp.autocast():
            output = model(inputs)
            loss = criterion(output, target)

        # Scales loss.  Calls backward() on scaled loss to
        # create scaled gradients.
        # Backward passes under autocast are not recommended.
        # Backward ops run in the same dtype autocast chose
        # for corresponding forward ops.
        scaler.scale(loss).backward()

        # scaler.step() first unscales the gradients of
        # the optimizer's assigned params.
        # If these gradients do not contain infs or NaNs,
        # optimizer.step() is then called,
        # otherwise, optimizer.step() is skipped.
        scaler.step(optimizer)

        # Updates the scale for next iteration.
        scaler.update()

        epoch_loss += loss.item()

    print(f"Epoch {epoch}. "
          f"Training loss: {epoch_loss / len(train_dataloader):.6f}")

    # Test
    avg_psnr = 0
    with torch.no_grad():
        for batch in val_dataloader:
            inputs, target = batch[0].to(device), batch[1].to(device)

            out = model(inputs)
            loss = criterion(out, target)
            psnr = 10 * math.log10(1 / loss.item())
            avg_psnr += psnr
    print(f"Average PSNR: {avg_psnr / len(val_dataloader):.2f} dB.")

    # Save model
    torch.save(model.state_dict(), f"weights/model_{epoch}.pth")
    if avg_psnr > best_psnr:
        best_psnr = avg_psnr
        torch.save(model.state_dict(),
                   f"weights/srcnn_X{args.scale_factor}.pth")