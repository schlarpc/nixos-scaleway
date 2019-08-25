{ config, lib, pkgs, ... }:
{
    imports = [
        <nixpkgs/nixos/modules/profiles/qemu-guest.nix>
    ];
    boot.initrd.availableKernelModules = [ "ata_piix" "virtio_pci" "virtio_blk" ];
    boot.kernelModules = [ "kvm-intel" "kvm-amd" ];
    fileSystems = {
        "/" = {
            device = "/dev/disk/by-label/nixos";
            fsType = "ext4";
        };
        "/boot" = {
            device = "/dev/disk/by-label/boot";
            fsType = "vfat";
        };
    };
    nix.maxJobs = lib.mkDefault 1;
}
