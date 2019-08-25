{ config, pkgs, ... }:
{
    imports = [
        ./hardware-configuration.nix
        ./scaleway-config.nix
    ];
    system.stateVersion = "19.03";
}
