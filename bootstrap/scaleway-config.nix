{ ... }:
{
    boot.loader.systemd-boot.enable = true;
    boot.loader.efi.canTouchEfiVariables = true;
    boot.kernelParams = [
        "console=ttyS0"
    ];
    boot.growPartition = true;
    fileSystems."/".autoResize = true;
}
