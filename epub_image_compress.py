#!/usr/bin/env python3
"""Compress embedded images in EPUB files: convert to JPEG and scale down."""

from __future__ import annotations

import argparse
import io
import os
import re
import sys
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from PIL import Image

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:
    pillow_heif = None

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".tif"}
DEFAULT_MAX_WIDTH = 1920
DEFAULT_MAX_HEIGHT = 1080
DEFAULT_JPEG_QUALITY = 85

NAMESPACES = {
    "container": "urn:oasis:names:tc:opendocument:xmlns:container",
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
}


def fit_within_box(width: int, height: int, max_w: int, max_h: int) -> tuple[int, int]:
    if width <= max_w and height <= max_h:
        return width, height
    ratio = min(max_w / width, max_h / height)
    return max(1, int(width * ratio)), max(1, int(height * ratio))


def normalize_root(path: Path) -> Path:
    """Return a canonical absolute path (macOS /var vs /private/var safe)."""
    return path.resolve()


def relative_posix(path: Path, root: Path) -> str:
    return path.resolve().relative_to(normalize_root(root)).as_posix()


def find_opf_path(epub_root: Path) -> Path:
    container_path = epub_root / "META-INF" / "container.xml"
    if not container_path.is_file():
        raise FileNotFoundError("META-INF/container.xml not found")

    tree = ET.parse(container_path)
    root = tree.getroot()
    rootfile = root.find(".//container:rootfile", NAMESPACES)
    if rootfile is None:
        raise FileNotFoundError("OPF rootfile not found in container.xml")

    opf_rel = rootfile.attrib.get("full-path")
    if not opf_rel:
        raise FileNotFoundError("OPF full-path attribute is missing")

    opf_path = epub_root / opf_rel
    if not opf_path.is_file():
        raise FileNotFoundError(f"OPF file not found: {opf_rel}")

    return opf_path


def collect_image_paths(epub_root: Path, opf_path: Path) -> set[Path]:
    images: set[Path] = set()
    root = normalize_root(epub_root)

    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS:
            images.add(path.resolve())

    tree = ET.parse(opf_path)
    opf_dir = opf_path.parent.resolve()

    for item in tree.getroot().findall(".//opf:manifest/opf:item", NAMESPACES):
        media_type = item.attrib.get("media-type", "")
        href = item.attrib.get("href")
        if not href or not media_type.startswith("image/"):
            continue
        candidate = (opf_dir / href).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            continue
        if candidate.is_file():
            images.add(candidate)

    return images


def process_image(
    data: bytes,
    ext: str,
    max_width: int,
    max_height: int,
    jpeg_quality: int,
) -> tuple[bytes | None, bool]:
    """Return (jpeg_bytes, changed). If unchanged, jpeg_bytes is None."""
    with Image.open(io.BytesIO(data)) as img:
        original_w, original_h = img.size
        target_w, target_h = fit_within_box(original_w, original_h, max_width, max_height)
        needs_resize = (target_w, target_h) != (original_w, original_h)
        is_jpeg = img.format in {"JPEG", "MPO"} and ext.lower() in {".jpg", ".jpeg"}

        if is_jpeg and not needs_resize:
            return None, False

        working = img
        if working.mode in ("RGBA", "LA", "P"):
            background = Image.new("RGB", working.size, (255, 255, 255))
            if working.mode == "P":
                working = working.convert("RGBA")
            if working.mode in ("RGBA", "LA"):
                background.paste(working, mask=working.split()[-1])
            else:
                background.paste(working)
            working = background
        elif working.mode != "RGB":
            working = working.convert("RGB")

        if needs_resize:
            working = working.resize((target_w, target_h), Image.Resampling.LANCZOS)

        out = io.BytesIO()
        working.save(out, format="JPEG", quality=jpeg_quality, optimize=True)
        return out.getvalue(), True


def replace_references(epub_root: Path, old_href: str, new_href: str) -> int:
    """Replace image path references in XHTML/HTML/CSS/NCX files."""
    if old_href == new_href:
        return 0

    old_name = Path(old_href).name
    new_name = Path(new_href).name
    replacements = 0
    text_suffixes = {".xhtml", ".html", ".htm", ".css", ".ncx"}

    for path in epub_root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in text_suffixes:
            continue

        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue

        updated = content.replace(old_href, new_href)

        if old_name != new_name:
            updated = re.sub(
                rf'((?:src|href|xlink:href)=["\'])([^"\']*?){re.escape(old_name)}(["\'])',
                rf"\1\2{new_name}\3",
                updated,
                flags=re.IGNORECASE,
            )
            updated = re.sub(
                rf'(url\(["\']?)([^"\')]*?){re.escape(old_name)}(["\')])',
                rf"\1\2{new_name}\3",
                updated,
                flags=re.IGNORECASE,
            )

        if updated != content:
            path.write_text(updated, encoding="utf-8")
            replacements += 1

    return replacements


def update_opf_manifest(opf_path: Path, old_href: str, new_href: str) -> None:
    if old_href == new_href:
        return

    tree = ET.parse(opf_path)
    root = tree.getroot()
    changed = False

    for item in root.findall(".//opf:manifest/opf:item", NAMESPACES):
        href = item.attrib.get("href")
        if href != old_href:
            continue
        item.set("href", new_href)
        item.set("media-type", "image/jpeg")
        changed = True

    if changed:
        ET.register_namespace("", NAMESPACES["opf"])
        ET.register_namespace("dc", NAMESPACES["dc"])
        tree.write(opf_path, encoding="utf-8", xml_declaration=True)


def pack_epub(source_dir: Path, output_path: Path) -> None:
    mimetype_path = source_dir / "mimetype"
    if not mimetype_path.is_file():
        raise FileNotFoundError("mimetype file not found in EPUB")

    with zipfile.ZipFile(output_path, "w") as archive:
        archive.writestr(
            "mimetype",
            mimetype_path.read_text(encoding="utf-8"),
            compress_type=zipfile.ZIP_STORED,
        )

        for root, _, files in os.walk(source_dir):
            for filename in files:
                if filename == "mimetype":
                    continue
                filepath = Path(root) / filename
                arcname = filepath.relative_to(source_dir).as_posix()
                archive.write(filepath, arcname, compress_type=zipfile.ZIP_DEFLATED)


def extract_epub(epub_path: Path, dest_dir: Path) -> None:
    with zipfile.ZipFile(epub_path, "r") as archive:
        archive.extractall(dest_dir)


def compress_epub(
    input_path: Path,
    output_path: Path,
    max_width: int,
    max_height: int,
    jpeg_quality: int,
    verbose: bool = False,
) -> dict[str, int]:
    stats = {"processed": 0, "skipped": 0, "failed": 0}

    with tempfile.TemporaryDirectory() as tmp:
        work_dir = normalize_root(Path(tmp))
        extract_epub(input_path, work_dir)

        opf_path = find_opf_path(work_dir)
        opf_dir = opf_path.parent.resolve()
        image_paths = sorted(collect_image_paths(work_dir, opf_path))

        if verbose:
            print(f"Found {len(image_paths)} image(s)", file=sys.stderr)

        for image_path in image_paths:
            image_path = image_path.resolve()
            rel_path = relative_posix(image_path, work_dir)
            ext = image_path.suffix.lower()

            try:
                original_data = image_path.read_bytes()
                jpeg_data, changed = process_image(
                    original_data, ext, max_width, max_height, jpeg_quality
                )
            except Exception as exc:
                stats["failed"] += 1
                hint = ""
                if pillow_heif is None and image_path.read_bytes()[:12].find(b"ftypheic") >= 0:
                    hint = " (HEIC image; install pillow-heif)"
                print(f"Warning: failed to process {rel_path}: {exc}{hint}", file=sys.stderr)
                continue

            if not changed:
                stats["skipped"] += 1
                if verbose:
                    print(f"Skipped (no change): {rel_path}", file=sys.stderr)
                continue

            new_rel = rel_path
            if ext not in {".jpg", ".jpeg"}:
                new_rel = str(Path(rel_path).with_suffix(".jpg"))
                new_path = work_dir / new_rel
                new_path.parent.mkdir(parents=True, exist_ok=True)
                new_path.write_bytes(jpeg_data)
                image_path.unlink()

                old_href = image_path.relative_to(opf_dir).as_posix()
                new_href = (work_dir / new_rel).resolve().relative_to(opf_dir).as_posix()
                update_opf_manifest(opf_path, old_href, new_href)
                replace_references(work_dir, old_href, new_href)
            else:
                image_path.write_bytes(jpeg_data)

            stats["processed"] += 1
            if verbose:
                print(f"Processed: {rel_path} -> {new_rel}", file=sys.stderr)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        pack_epub(work_dir, output_path)

    return stats


def default_output_path(input_path: Path) -> Path:
    return input_path.with_name(f"{input_path.stem}_compressed{input_path.suffix}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert embedded EPUB images to JPEG and scale them down."
    )
    parser.add_argument("input", type=Path, help="Input EPUB file")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Output EPUB file (default: <input>_compressed.epub)",
    )
    parser.add_argument(
        "--max-width",
        type=int,
        default=DEFAULT_MAX_WIDTH,
        help=f"Maximum image width (default: {DEFAULT_MAX_WIDTH})",
    )
    parser.add_argument(
        "--max-height",
        type=int,
        default=DEFAULT_MAX_HEIGHT,
        help=f"Maximum image height (default: {DEFAULT_MAX_HEIGHT})",
    )
    parser.add_argument(
        "--quality",
        type=int,
        default=DEFAULT_JPEG_QUALITY,
        help=f"JPEG quality 1-95 (default: {DEFAULT_JPEG_QUALITY})",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    input_path: Path = args.input
    if not input_path.is_file():
        print(f"Error: input file not found: {input_path}", file=sys.stderr)
        return 1

    if args.quality < 1 or args.quality > 95:
        print("Error: --quality must be between 1 and 95", file=sys.stderr)
        return 1

    output_path = args.output or default_output_path(input_path)

    try:
        stats = compress_epub(
            input_path=input_path,
            output_path=output_path,
            max_width=args.max_width,
            max_height=args.max_height,
            jpeg_quality=args.quality,
            verbose=args.verbose,
        )
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(
        f"Done: {stats['processed']} processed, "
        f"{stats['skipped']} skipped, {stats['failed']} failed"
    )
    print(f"Output: {output_path}")
    return 0 if stats["failed"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
