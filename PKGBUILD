# Maintainer: madky
pkgname=umbral
pkgver=0.8.5
pkgrel=1
pkgdesc="Minimal Battle.net launcher for Arch/Hyprland: official client, WoW Forever, umu + Proton (GTK4/libadwaita)"
arch=('any')
url='https://github.com/madkyp/umbral-project'
license=('MIT')
depends=('python' 'python-gobject' 'python-pillow' 'gtk4' 'libadwaita' 'umu-launcher' 'pciutils' 'vulkan-tools'
         'vulkan-icd-loader' 'lib32-vulkan-icd-loader' 'libnotify' 'tar' 'zstd')
optdepends=('gamemode: CPU tweaks while playing'
            'lib32-gamemode: gamemode for 32-bit processes'
            'gamescope: optional micro-compositor'
            'mangohud: performance overlay'
            'lib32-mangohud: overlay for 32-bit games'
            'winetricks: optional prefix dependencies'
            'hyprland: floating window rule and snippet validation')
# Built from the project tree (clone the repo, then run makepkg -si).
source=()

package() {
  cd "$startdir"
  install -d "$pkgdir/usr/lib/umbral"
  cp -r umbral "$pkgdir/usr/lib/umbral/"
  find "$pkgdir/usr/lib/umbral" -name '__pycache__' -prune -exec rm -rf {} +
  python -m compileall -q -d /usr/lib/umbral "$pkgdir/usr/lib/umbral"
  install -Dm755 data/umbral "$pkgdir/usr/bin/umbral"
  install -Dm644 data/dev.madky.Umbral.desktop "$pkgdir/usr/share/applications/dev.madky.Umbral.desktop"
  install -Dm644 data/dev.madky.Umbral.svg "$pkgdir/usr/share/icons/hicolor/scalable/apps/dev.madky.Umbral.svg"
  install -Dm644 LICENSE "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
}
