"""Print .mesh and .anim files, or convert them to txt, json or xml: python -m <folder> convert_to -i <file>."""

import argparse
import json
import xml.etree.ElementTree as Xml
from pathlib import Path

from .pdx_data import PDXData, PDXDataJSON, read_meshfile


def convert_to(inpath, outpath, out_format):
    files = []
    out_folder = None
    suffix = f".{out_format}" if out_format else ""

    inpath = Path(inpath)
    if inpath.is_file():
        try:
            out_filepath = Path(outpath)
        except TypeError:
            out_filepath = inpath.parent / inpath.name
        out_folder = out_filepath.parent
        files.append([inpath, out_filepath.with_suffix(suffix)])

    elif inpath.is_dir():
        try:
            out_folder = Path(outpath)
        except TypeError:
            out_folder = inpath

        for ext in ["*.mesh", "*.anim"]:
            for fullpath in inpath.rglob(ext):
                files.append([fullpath, (out_folder / fullpath.relative_to(inpath)).with_suffix(suffix)])

    for i, (in_file, out_file) in enumerate(files):
        pdx_Xml = read_meshfile(f"{in_file}")
        pdx_Data = PDXData(pdx_Xml)
        if out_format:
            print(f"{i + 1}/{len(files)} : {in_file.relative_to(inpath.parent)} --> {out_file.relative_to(out_folder)}")
            out_file.parent.mkdir(parents=True, exist_ok=True)
            if out_format == "txt":
                with open(f"{out_file}", "wt") as fp:
                    fp.write(f"{pdx_Data}\n")
            if out_format == "json":
                with open(f"{out_file}", "wt") as fp:
                    json.dump(pdx_Data, fp, indent=2, cls=PDXDataJSON)
            if out_format == "xml":
                tree = Xml.ElementTree(pdx_Xml)
                tree.write(f"{out_file}")
        else:
            print("-" * 120)
            print(f"{i + 1}/{len(files)} : {in_file.relative_to(inpath.parent)}", end="\n")
            print(f"{pdx_Data}")


def main():
    parser = argparse.ArgumentParser(prog="python -m <package folder>")
    commands = parser.add_subparsers(dest="command", required=True)
    convert = commands.add_parser("convert_to")
    convert.add_argument("-i", "--inpath", required=True)
    convert.add_argument("-o", "--outpath", default=None)
    convert.add_argument("-f", "--format", dest="out_format", choices=["txt", "json", "xml"], default="")
    args = parser.parse_args()
    convert_to(args.inpath, args.outpath, args.out_format)


if __name__ == "__main__":
    main()
