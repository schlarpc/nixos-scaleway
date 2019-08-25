#!/bin/bash

NIXOS_CHANNEL=19.03

set -exo pipefail

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y \
    -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confnew \
    bzip2 parted dosfstools sudo

# prepare partitions
parted /dev/vdb -s -- mklabel gpt
parted /dev/vdb -- mkpart primary 512MiB 100%
parted /dev/vdb -- mkpart ESP fat32 1MiB 512MiB
parted /dev/vdb -- set 2 boot on
partprobe
mkfs.ext4 -L nixos /dev/vdb1
mkfs.fat -F 32 -n boot /dev/vdb2
mount /dev/disk/by-label/nixos /mnt
mkdir -p /mnt/boot
mount /dev/disk/by-label/boot /mnt/boot

# nix install requires nixbld group and to be able to use sudo
groupadd -g 30000 nixbld
useradd -u 30000 -g nixbld -G nixbld nixbld
echo 'root ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/10-root

# prepare local install of nix
curl https://nixos.org/nix/install | sh
source "$HOME/.nix-profile/etc/profile.d/nix.sh"
nix-channel --add "https://nixos.org/channels/nixos-$NIXOS_CHANNEL" nixos
nix-channel --remove nixpkgs
nix-channel --update

# install nixos to the prepped partition
mkdir -p /mnt/etc/nixos
cp $(dirname "$0")/*.nix /mnt/etc/nixos
export NIX_PATH="nixpkgs=$HOME/.nix-defexpr/channels/nixos"
nix-env -iE '_: with import <nixpkgs/nixos> { configuration = {}; }; config.system.build.nixos-install'
nixos-install --root /mnt --no-root-passwd

# use instance stop to signal completion to make-nixos-image
shutdown -P now
