import hashlib, zipfile, sys
from pathlib import Path
stage = Path(sys.argv[1])
zip_path = Path(sys.argv[2])
dirs = [d for d in stage.iterdir() if d.is_dir()]
if len(dirs) != 1:
    raise SystemExit("в каталоге сборки должна быть ровно одна папка программы")
root = dirs[0]
zip_path.parent.mkdir(parents=True, exist_ok=True)
if zip_path.exists():
    zip_path.unlink()
with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
    for f in sorted(root.rglob("*")):
        if f.is_file():
            zf.write(f, f.relative_to(stage).as_posix())
h = hashlib.sha256(zip_path.read_bytes()).hexdigest()
zip_path.with_name(zip_path.name + ".sha256").write_text(f"{h}  {zip_path.name}\n")
print(h, zip_path)
