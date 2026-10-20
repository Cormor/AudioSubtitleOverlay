"""Generate the code-native vector identity and Windows icon sizes."""

from pathlib import Path

from PIL import Image, ImageDraw


def main():
    root = Path(__file__).resolve().parents[1] / "assets" / "brand"
    root.mkdir(parents=True, exist_ok=True)
    root.joinpath("icon.svg").write_text('''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128"><title>声幕：声音成为字幕</title><rect width="128" height="128" rx="28" fill="#10243a"/><path d="M38 94 30 109 57 94H92Q108 94 108 78V38Q108 22 92 22H36Q20 22 20 38V78Q20 94 38 94Z" fill="none" stroke="#39d7ed" stroke-width="6" stroke-linejoin="round"/><path d="M30 56H37L43 39 50 72 57 48 63 56H68" fill="none" stroke="#39d7ed" stroke-width="5" stroke-linecap="round" stroke-linejoin="round"/><path d="M78 43H96M78 56H96M78 69H90" stroke="#f1f7ff" stroke-width="5" stroke-linecap="round"/></svg>''', encoding="utf-8")
    scale = 4
    image = Image.new("RGBA", (128 * scale, 128 * scale))
    draw = ImageDraw.Draw(image)
    def box(coords): return tuple(round(x * scale) for x in coords)
    draw.rounded_rectangle(box((0, 0, 127, 127)), radius=28*scale, fill="#10243a")
    draw.rounded_rectangle(box((20,22,108,94)),radius=16*scale,outline="#39d7ed",width=6*scale)
    draw.line([box(p) for p in [(38,92),(30,109),(57,94)]], fill="#39d7ed", width=6*scale, joint="curve")
    draw.line([box(p) for p in [(30,56),(37,56),(43,39),(50,72),(57,48),(63,56),(68,56)]], fill="#39d7ed",width=5*scale,joint="curve")
    for y,end in [(43,96),(56,96),(69,90)]:
        draw.line(box((78,y,end,y)),fill="#f1f7ff",width=5*scale)
    image = image.resize((256,256),Image.Resampling.LANCZOS)
    image.save(root / "icon.png")
    image.save(root / "icon.ico",sizes=[(16,16),(20,20),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])


if __name__ == "__main__":
    main()
