#!/usr/bin/env python3
"""Export a repo-native SVG to web/PWA and optional macOS assets.
Usage: python3 scripts/export-app-icons.py path/to/icon.svg [--macos]
Requires rsvg-convert; macOS iconutil for --macos. No runtime dependency.
"""
import argparse
import subprocess
import tempfile
from pathlib import Path
import xml.etree.ElementTree as ET


def export(source, macos=False):
    source = Path(source)
    folder = source.parent
    original = source.read_text()
    svg = ET.fromstring(original)
    box = svg.attrib.get('viewBox', '0 0 512 512').split()
    width, height = float(box[2]), float(box[3])
    # Full-bleed background + inset identity keeps the mark within the maskable safe circle.
    inner = original[original.index('>') + 1:original.rindex('</svg>')]
    first_rect = svg.find('{http://www.w3.org/2000/svg}rect')
    fill = first_rect.get('fill', '#182c46') if first_rect is not None else '#182c46'
    colors = [fill if fill.startswith('#') else '#1b5aa8', '#b45309']
    for demo, color in [(False, colors[0]), (True, colors[1])]:
        prefix = 'demo-' if demo else ''
        badge = ('<circle cx="50" cy="50" r="10" fill="#fff"/><path d="M46 44h4c8 0 8 12 0 12h-4z M49 47v6h1c4 0 4-6 0-6z" fill="#b45309" fill-rule="evenodd"/>') if demo else ''
        full = f'<svg xmlns="http://www.w3.org/2000/svg" width="512" height="512" viewBox="0 0 64 64"><rect width="64" height="64" rx="14" fill="{color}"/><svg x="4" y="4" width="56" height="56" viewBox="0 0 {width:g} {height:g}">{inner}</svg>{badge}</svg>'
        if not demo:
            full = original
        mask = f'<svg xmlns="http://www.w3.org/2000/svg" width="512" height="512" viewBox="0 0 64 64"><rect width="64" height="64" fill="{color}"/><svg x="10" y="10" width="44" height="44" viewBox="0 0 {width:g} {height:g}">{inner}</svg>{badge}</svg>'
        if demo:
            (folder / 'demo-icon.svg').write_text(full)
        (folder / f'{prefix}icon-maskable.svg').write_text(mask)
        with tempfile.TemporaryDirectory() as tmp:
            temp = Path(tmp) / 'render.svg'
            for name, size, body in [('icon-192.png',192,full),('icon-512.png',512,full),('apple-touch-icon.png',180,mask),('icon-maskable-512.png',512,mask),('favicon-32.png',32,full)]:
                temp.write_text(body)
                subprocess.run(['rsvg-convert','-w',str(size),'-h',str(size),str(temp),'-o',str(folder / (prefix+name))],check=True)
            if macos:
                iconset = Path(tmp) / 'AppIcon.iconset'
                iconset.mkdir()
                temp.write_text(full)
                for size in [16,32,128,256,512]:
                    for scale in [1,2]:
                        name=f'icon_{size}x{size}'+('@2x' if scale==2 else '')+'.png'
                        subprocess.run(['rsvg-convert','-w',str(size*scale),'-h',str(size*scale),str(temp),'-o',str(iconset/name)],check=True)
                subprocess.run(['iconutil','-c','icns',str(iconset),'-o',str(folder/(prefix+'AppIcon.icns'))],check=True)


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source')
    parser.add_argument('--macos',action='store_true')
    args=parser.parse_args()
    export(args.source,args.macos)
