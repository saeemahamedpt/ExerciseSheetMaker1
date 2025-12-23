
# api/generate.py
from http.server import BaseHTTPRequestHandler
import json, os, io, sys
import requests
from PIL import Image

# --- A4 constants (300 DPI) ---
A4_DPI = 300
A4_W_PX = int(8.27 * A4_DPI)   # 2481 px
A4_H_PX = int(11.69 * A4_DPI)  # 3507 px

# --- Helpers: download, fit with UPSCALING, auto orientation, make PDF bytes ---
def download_image(url: str, timeout=(5, 20)) -> Image.Image:
    """
    Download image bytes; set a basic User-Agent to reduce hotlink blocks.
    Returns a Pillow RGB Image.
    """
    headers = {"User-Agent": "Mozilla/5.0 (compatible; VercelPython/1.0)"}
    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    img = Image.open(io.BytesIO(r.content))
    if img.mode != "RGB":
        img = img.convert("RGB")
    return img

def fit_to_a4_allow_upscale(img: Image.Image, orientation="portrait", margin_ratio=0.03) -> Image.Image:
    """
    Place 'img' on a white A4 page, maximizing size within margins.
    - Preserves aspect ratio
    - Allows UPSCALING (unlike thumbnail)
    - No cropping or stretching
    """
    if orientation == "landscape":
        page_w, page_h = A4_H_PX, A4_W_PX
    else:  # default portrait
        page_w, page_h = A4_W_PX, A4_H_PX

    # clamp margin ratio (0%..20%)
    margin_ratio = max(0.0, min(0.2, float(margin_ratio)))
    margin = int(margin_ratio * page_w)
    max_w = page_w - 2 * margin
    max_h = page_h - 2 * margin

    iw, ih = img.width, img.height
    if iw <= 0 or ih <= 0:
        raise ValueError("Invalid image size.")

    scale = min(max_w / iw, max_h / ih)
    target_w = max(1, int(iw * scale))
    target_h = max(1, int(ih * scale))

    resized = img.resize((target_w, target_h), Image.LANCZOS)

    # Center on white canvas
    canvas = Image.new("RGB", (page_w, page_h), "white")
    x = (page_w - target_w) // 2
    y = (page_h - target_h) // 2
    canvas.paste(resized, (x, y))
    return canvas

def choose_orientation_auto(img: Image.Image, threshold=1.0) -> str:
    """
    If image is wide (aspect >= threshold), use landscape, else portrait.
    """
    aspect = img.width / img.height if img.height else 1.0
    return "landscape" if aspect >= threshold else "portrait"

def make_pdf_bytes(pages, dpi=A4_DPI) -> bytes:
    """
    Create multi-page PDF in-memory. Each element in 'pages' must be an RGB Pillow Image.
    """
    if not pages:
        raise ValueError("No pages to save.")
    buf = io.BytesIO()
    first, rest = pages[0], pages[1:]
    first.save(buf, "PDF", resolution=dpi, save_all=True, append_images=rest)
    return buf.getvalue()

# --- SerpAPI: get the first N image URLs (Google Images) ---
def google_images_serpapi(query: str, num: int, api_key: str):
    """
    Use SerpAPI's Google Images endpoint to get image URLs.
    Takes the first 'num' results.
    """
    if not api_key:
        raise RuntimeError("Missing SERPAPI_KEY environment variable.")

    # clamp requested images (1..20)
    num = max(1, min(20, int(num or 6)))

    url = "https://serpapi.com/search"
    params = {
        "engine": "google_images",
        "q": query,
        "num": num,
        "api_key": api_key,
        # You can uncomment to filter adult content:
        # "safe": "active",
    }
    r = requests.get(url, params=params, timeout=(5, 20))
    r.raise_for_status()
    data = r.json()

    results = data.get("images_results", []) or []
    urls = []
    for item in results[:num]:
        u = item.get("original") or item.get("thumbnail")
        if u:
            urls.append(u)
    return urls

# --- Response helpers ---
def _write_json(handler: BaseHTTPRequestHandler, obj, code=200):
    body = json.dumps(obj).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(body)

def _write_pdf(handler: BaseHTTPRequestHandler, pdf_bytes: bytes, filename: str):
    handler.send_response(200)
    handler.send_header("Content-Type", "application/pdf")
    handler.send_header("Content-Disposition", f'attachment; filename="{filename}"')
    handler.send_header("Content-Length", str(len(pdf_bytes)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(pdf_bytes)

# --- HTTP handler ---
class handler(BaseHTTPRequestHandler):
    def log(self, *args):
        try:
            print(*args, file=sys.stderr, flush=True)
        except Exception:
            pass

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.end_headers()

    def do_GET(self):
        # Health check endpoint
        _write_json(self, {"status": "ok", "endpoint": "/api/generate"}, 200)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length > 0 else b"{}"

            # Parse JSON body
            try:
                data = json.loads(raw or b"{}")
            except Exception:
                return _write_json(self, {"error": "Invalid JSON body"}, 400)

            query = (data.get("querytext") or "").strip()
            num_images = int(data.get("num_images", 6))
            orientation = (data.get("orientation") or "auto").strip().lower()
            margin_ratio = float(data.get("margin_ratio", 0.03))
            filename = (data.get("filename") or "google_images.pdf").strip()

            serp_key = os.environ.get("SERPAPI_KEY", "").strip()
            if not serp_key:
                return _write_json(self, {"error": "Missing SERPAPI_KEY env var"}, 500)
            if not query:
                return _write_json(self, {"error": "querytext is required"}, 400)

            urls = google_images_serpapi(query, num_images, serp_key)
            if not urls:
                return _write_json(self, {"error": "No image URLs returned"}, 502)

            pages = []
            for u in urls:
                try:
                    img = download_image(u)
                    orient = choose_orientation_auto(img) if orientation == "auto" else orientation
                    page = fit_to_a4_allow_upscale(img, orientation=orient, margin_ratio=margin_ratio)
                    pages.append(page)
                except Exception as e:
                    self.log(f"Image skipped ({u}): {e}")

            if not pages:
                return _write_json(self, {"error": "No pages created"}, 502)

            pdf_bytes = make_pdf_bytes(pages, dpi=A4_DPI)
            _write_pdf(self, pdf_bytes, filename)

        except Exception as e:
            _write_json(self, {"error": "Internal error", "detail": str(e)}, 500)
