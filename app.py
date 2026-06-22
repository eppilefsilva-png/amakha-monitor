import os, re, json, hashlib, time, requests
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ── Credenciais Shopee ──────────────────────────
SHOPEE_APP_ID = os.environ.get("SHOPEE_APP_ID", "18319120783")
SHOPEE_SECRET = os.environ.get("SHOPEE_SECRET", "UD6OGFEYZGAFJXWWM3HPHH6LGYZK5P7O")
SHOPEE_URL    = "https://open-api.affiliate.shopee.com.br/graphql"

# ── Precos minimos ──────────────────────────────
PRECO_MIN = {"15ml": 31.50, "100ml": 146.00, "unknown": 15.00}

# ── Lojas oficiais ignoradas ────────────────────
LOJAS_OFICIAIS = {
    "amakha paris", "amakha oficial", "amakha paris oficial",
    "loja oficial amakha", "amakhaparis", "amakha_paris",
    "amakha paris shopee", "amakha official store", "amakha paris store",
}

# ── Headers ML ─────────────────────────────────
ML_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "pt-BR,pt;q=0.9",
    "Origin": "https://www.mercadolivre.com.br",
    "Referer": "https://www.mercadolivre.com.br/",
}

# ═══════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════
def detectar_tamanho(titulo):
    t = titulo.lower()
    if re.search(r"100\s*ml|\b100\b|grande|family|jumbo", t): return "100ml"
    if re.search(r"15\s*ml|\b15\b|pequeno|mini|travel|bolsa", t):  return "15ml"
    return "unknown"

def eh_loja_oficial(vendedor):
    v = vendedor.lower().strip()
    return any(o in v or v in o for o in LOJAS_OFICIAIS)

def montar(plat, titulo, preco, vendedor, url):
    tam     = detectar_tamanho(titulo)
    oficial = eh_loja_oficial(vendedor)
    susp    = False if oficial else (preco > 0 and preco < PRECO_MIN.get(tam, 15))
    diff    = round(PRECO_MIN.get(tam, 0) - preco, 2) if tam != "unknown" and preco > 0 else 0
    return dict(plataforma=plat, titulo=titulo, preco=preco, tamanho=tam,
                suspeito=susp, oficial=oficial, vendedor=vendedor, url=url, diff=diff)

def assinar_shopee(timestamp, payload):
    raw = f"{SHOPEE_APP_ID}{timestamp}{payload}{SHOPEE_SECRET}"
    return hashlib.sha256(raw.encode()).hexdigest()

# ═══════════════════════════════════════════════
#  ROTAS
# ═══════════════════════════════════════════════
@app.route("/health")
def health():
    return jsonify({"status": "ok", "version": "2.0"})

@app.route("/ml")
def buscar_ml():
    kw   = request.args.get("q", "Amakha Paris")
    sess = requests.Session()
    try:
        sess.get("https://www.mercadolivre.com.br/", headers=ML_HEADERS, timeout=8)
    except: pass

    produtos, vistos = [], set()
    for offset in [0, 50]:
        url = f"https://api.mercadolibre.com/sites/MLB/search?q={requests.utils.quote(kw)}&limit=50&offset={offset}"
        try:
            r = sess.get(url, headers=ML_HEADERS, timeout=12)
            if r.status_code != 200:
                continue
            for item in r.json().get("results", []):
                titulo = item.get("title", "")
                if "amakha" not in titulo.lower(): continue
                pid = item.get("id", "")
                if pid in vistos: continue
                vistos.add(pid)
                preco   = float(item.get("price", 0))
                vendedor= item.get("seller", {}).get("nickname", "—")
                url_prod= item.get("permalink", "")
                produtos.append(montar("Mercado Livre", titulo, preco, vendedor, url_prod))
        except: pass
        time.sleep(0.3)
    return jsonify(produtos)

@app.route("/shopee")
def buscar_shopee():
    kw    = request.args.get("q", "Amakha Paris")
    query = """
    query ProductOfferV2($keyword: String, $sortType: Int, $page: Int, $limit: Int) {
        productOfferV2(keyword: $keyword, sortType: $sortType, page: $page, limit: $limit) {
            nodes {
                itemId productName productLink offerLink
                priceMin sales ratingStar shopName shopId commissionRate
            }
            pageInfo { page limit hasNextPage }
        }
    }
    """
    variables = {"keyword": kw, "sortType": 4, "page": 1, "limit": 50}
    payload   = json.dumps({"query": query, "variables": variables}, separators=(",", ":"))
    timestamp = str(int(time.time()))
    sig       = assinar_shopee(timestamp, payload)
    headers   = {
        "Authorization": f"SHA256 Credential={SHOPEE_APP_ID}, Signature={sig}, Timestamp={timestamp}",
        "Content-Type":  "application/json",
        "User-Agent":    "Mozilla/5.0",
    }
    try:
        r = requests.post(SHOPEE_URL, headers=headers, data=payload, timeout=20)
        if r.status_code != 200:
            return jsonify({"error": f"HTTP {r.status_code}", "detail": r.text[:200]}), 502
        data  = r.json()
        if "errors" in data:
            return jsonify({"error": "GraphQL", "detail": str(data["errors"])}), 502
        nodes = data.get("data", {}).get("productOfferV2", {}).get("nodes", [])
        prods = []
        for item in nodes:
            titulo = item.get("productName", "")
            if "amakha" not in titulo.lower(): continue
            preco  = float(item.get("priceMin", 0) or 0)
            url    = item.get("offerLink") or item.get("productLink", "")
            vend   = item.get("shopName", "—")
            prods.append(montar("Shopee", titulo, preco, vend, url))
        return jsonify(prods)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/buscar")
def buscar_tudo():
    kw = request.args.get("q", "Amakha Paris")
    ml = buscar_ml().get_json() or []
    sh = buscar_shopee().get_json() or []
    if isinstance(ml, dict): ml = []
    if isinstance(sh, dict): sh = []
    todos = ml + sh
    todos.sort(key=lambda p: (-p.get("suspeito", 0), -p.get("diff", 0)))
    return jsonify(todos)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
