# Lokal aus dem Git-Ordner bauen und installieren:  makepkg -si
# (Einfacher ohne makepkg: sudo sh install.sh)
pkgname=netshift
pkgver=1.1.5
pkgrel=1
pkgdesc="System-Snapshots wie Timeshift (rsync) – auch auf SMB-, NFS- und SSH-Netzwerkziele"
arch=('any')
license=('GPL-3.0-or-later')
depends=('python' 'rsync' 'e2fsprogs' 'util-linux' 'polkit')
optdepends=('python-gobject: moderne Oberfläche'
            'gtk4: moderne Oberfläche'
            'libadwaita: moderne Oberfläche'
            'tk: klassische Oberfläche als Rückfall'
            'cifs-utils: SMB/Windows-Freigaben'
            'nfs-utils: NFS-Freigaben'
            'openssh: SSH-Ziele'
            'cryptsetup: verschlüsselte Images und Festplatten'
            'arch-install-scripts: GRUB-Aktualisierung nach Restore auf anderes Laufwerk'
            'xorg-xhost: Oberfläche unter Wayland')

package() {
    cd "$startdir"
    DESTDIR="$pkgdir" sh tools/install-files.sh
}
