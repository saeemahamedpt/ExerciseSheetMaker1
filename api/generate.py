
# api/generate.py
from http.server import BaseHTTPRequestHandler
import json, os, io
import requests
from PIL import Image

# --- A4 constants (300 DPI) ---
A4_DPI = 300
A4_W_PX = int(8.27 * A4_DPI)  # 2481 px
A4_H_PX = int(11.69 * A4_DPI) # 3507 px

# --- Helpers: download, fit with UPSCALING, auto orientation, make PDF bytes ---
def download_image(url, timeout=(5, 20)):
    """
    Download image bytes; set a basic User-Agent to reduce hotlink blocks.
    """
    headers = {"User-Agent": "Mozilla/5.0 (compatible; VercelPython/1.0)"}
    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    img = Image.open(io.BytesIO(r.content))
    if img.mode != "RGB":
        img = img.convert("RGB")
    return img

def fit_to_a4_allow_upscale(img: Image.Image, orientation="portrait", margin_ratio=0.03):
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

    margin = int(margin_ratio * page_w)
    max_w = page_w - 2 * margin
    max_h = page_h - 2 * margin

    iw, ih = img.width, img.height
    if iw <= 0 or ih <= 0:
        raise ValueError("Invalid image size.")

    # Scale to fill as much as possible, preserving aspect ratio (allows upscaling)
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

def choose_orientation_auto(img: Image.Image, threshold=1.0):
    """
    If image is wide (aspect >= threshold), use landscape, else portrait.
    """
    aspect = img.width / img.height if img.height else 1.0
    return "landscape" if aspect >= threshold else "portrait"

def make_pdf_bytes(pages, dpi=A4_DPI):
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
def google_images_serpapi(query, num=6, api_key=None):
    """
    Use SerpAPI's Google Images endpoint to get image URLs.
    Takes the first 'num' results.
    """
    if not api_key:
        raise RuntimeError("Missing SERPAPI_KEY environment variable.")
    url = "https://serpapi.com/search"
    params = {
        "engine": "google_images",
        "q": query,
        "num": max(1, num),
        "api_key": api_key
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

# --- HTTP handler ---
class handler(BaseHTTPRequestHandler):
    def _send_json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.end_headers()

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length > 0 else b""
            data = json.loads(body or b"{}")

            query = (data.get("querytext") or "").strip()
            num_images = int(data.get("num_images", 6))
            orientation = data.get("orientation", "auto")
            margin_ratio = float(data.get("margin_ratio", 0.03))
            filename = (data.get("filename") or "google_images.pdf").strip()

            serp_key = os.environ.get("SERPAPI_KEY", "").strip()
            if not serp_key:
                return self._send_json({"error": "Missing SERPAPI_KEY env var"}, 500)
            if not query:
                return self._send_json({"error": "querytext is required"}, 400)

            urls = google_images_serpapi(query, num=num_images, api_key=serp_key)
            if not urls:
                return self._send_json({"error": "No image URLs returned"}, 502)

            pages = []
            for u in urls:
                try:
                    img = download_image(u)
                    orient = choose_orientation_auto(img) if orientation == "auto" else orientation
                    page = fit_to_a4_allow_upscale(img, orientation=orient, margin_ratio=margin_ratio)
                    pages.append(page)
                except Exception:
                    # Skip failed image downloads/conversions
                    pass

            if not pages:
                return self._send_json({"error": "No pages created"}, 502)

            pdf_bytes = make_pdf_bytes(pages, dpi=A4_DPI)
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(len(pdf_bytes)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(pdf_bytes)

        except Exception as e:
