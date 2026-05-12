import os
import sys
import io
import base64
import hashlib
import json
import urllib.request
import urllib.error
import webbrowser
from datetime import date, timedelta, datetime
from dotenv import load_dotenv
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException
import google.oauth2.credentials
import google.auth.transport.requests

load_dotenv()

DEVELOPER_TOKEN = os.getenv("DEVELOPER_TOKEN")
CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
REFRESH_TOKEN = os.getenv("REFRESH_TOKEN")
MCC_CUSTOMER_ID = os.getenv("MCC_CUSTOMER_ID")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO = os.getenv("GITHUB_REPO", "")  # "VeronikaFilipes/Anomalie"
_REPORT_PW = os.getenv("REPORT_PASSWORD", "")
PW_HASH = hashlib.sha256(_REPORT_PW.encode()).hexdigest() if _REPORT_PW else ""

ANOMALY_THRESHOLD = 0.20
EXCLUDED_ACCOUNTS_FILE = os.path.join(os.path.dirname(__file__), "excluded_accounts.txt")
ACCOUNT_OWNERS_FILE = os.path.join(os.path.dirname(__file__), "account_owners.txt")
MC_HISTORY_FILE = os.path.join(os.path.dirname(__file__), "mc_history.json")

# Definice barev pro každou metriku: True = zelená když roste, False = červená když roste
METRIC_HIGHER_IS_GOOD = {
    "pno":        False,  # PNO roste → červená, klesá → zelená
    "conv_value": True,   # Hodnota konverze roste → zelená, klesá → červená
    "ctr":        True,   # CTR roste → zelená
    "cost":       False,  # Cena roste → červená, klesá → zelená
    "cpc":        False,  # CPC roste → červená
}

MC_ACCOUNT_MAP = {
    "2965146730": "5310345312",  # BOHEMIA GLOVES
    "6338338711": "511817877",   # CarpCentrum.cz
    "1994994955": "100247485",   # Centrum-zatepleni.cz
    "6849157615": "554730244",   # Cyklopoint
    "5417966097": "113412544",   # Equimall
    "8090299543": "5500629062",  # FRAMICH.CZ
    "8046356825": "108667234",   # Formuleshop by FM
    "1400102268": "5079486292",  # George Technika
    "4648843530": "9251062",     # Gigamat.cz-POAS
    "2095924888": "5445879049",  # Gigamat.hu
    "3431773595": "321698590",   # Gigamat.sk
    "7345967873": "651184727",   # Lehkespani.cz
    "9810583182": "5714047726",  # Makatan
    "4915670484": "145405426",   # Matchaday
    "3646982543": "5448093532",  # Playlab.cz
    "9951842209": "255946984",   # Playlab.sk
    "5491448210": "117466186",   # RISESNU.CZ
    "4174529629": "339697141",   # ROZSVITIMESVET
    "8335443135": "5371446761",  # Sprchygelco.cz
    "4465019086": "5440256464",  # Srouby.net
    "9001211107": "287721650",   # Tinamoda
    "9822417303": "180211237",   # Topfuton.cz
    "2462565284": "5579203306",  # Trendie.cz
    "8357038978": "142471143",   # Trendie.sk
    "1179809815": "643371130",   # VAJANA
    "7610928280": "165344382",   # Zandup
    "6501577129": "125491657",   # bps-koupelny.cz
    "9832130774": "553300387",   # ezachranar.cz
    "6015927850": "5656440265",  # ezachranar.sk
    "4299625058": "605557518",   # fajnspanek.cz
    "1801216403": "5083707205",  # fotbal-shop.cz
    "9962932205": "5728241149",  # futons.sk
    "6875958613": "5671763060",  # hardsmile
    "3100746378": "169750512",   # kamerak.cz
    "6865556143": "747584517",   # nejlevnejsiautoradia
    "1107971921": "242468855",   # nejstany.cz
    "9575313703": "5781774581",  # novotny-tcm
    "3100385092": "242453464",   # noznicovystan.sk
    "3441827863": "137554366",   # partykostym.cz
    "5363683295": "5417710906",  # tristart.cz
    "2904287929": "8976404",     # Malyali
    "9577789742": "5592868012",  # Trendie.cz (novy)
    "4626346466": "000000000",   # ZP promo - unknown, skip
    "8641584692": "000000000",   # Zápecová - unknown, skip
}


def load_account_owners():
    owners = {}
    if not os.path.exists(ACCOUNT_OWNERS_FILE):
        return owners
    with open(ACCOUNT_OWNERS_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                cid, name = line.split("=", 1)
                owners[cid.strip().replace("-", "")] = name.strip()
    return owners


def load_excluded_accounts():
    if not os.path.exists(EXCLUDED_ACCOUNTS_FILE):
        return set()
    excluded = set()
    with open(EXCLUDED_ACCOUNTS_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                excluded.add(line.replace("-", ""))
    return excluded


def get_client():
    config = {
        "developer_token": DEVELOPER_TOKEN,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": REFRESH_TOKEN,
        "login_customer_id": MCC_CUSTOMER_ID,
        "use_proto_plus": True,
    }
    return GoogleAdsClient.load_from_dict(config)


def list_child_accounts(client):
    service = client.get_service("GoogleAdsService")
    query = """
        SELECT customer_client.id, customer_client.descriptive_name
        FROM customer_client
        WHERE customer_client.level = 1
          AND customer_client.status = 'ENABLED'
    """
    response = service.search(customer_id=MCC_CUSTOMER_ID, query=query)
    accounts = []
    for row in response:
        accounts.append({
            "id": str(row.customer_client.id),
            "name": row.customer_client.descriptive_name,
        })
    return accounts


def fetch_campaign_metrics(client, customer_id, days=7):
    service = client.get_service("GoogleAdsService")
    today = date.today()
    date_from = today - timedelta(days=days)
    query = f"""
        SELECT
            campaign.name,
            campaign.id,
            campaign.campaign_budget,
            campaign_budget.amount_micros,
            segments.date,
            metrics.cost_micros,
            metrics.clicks,
            metrics.impressions,
            metrics.ctr,
            metrics.conversions_value,
            metrics.average_cpc
        FROM campaign
        WHERE segments.date >= '{date_from.strftime('%Y-%m-%d')}'
          AND segments.date <= '{(today - timedelta(days=1)).strftime('%Y-%m-%d')}'
          AND campaign.status = 'ENABLED'
    """
    try:
        response = service.search(customer_id=customer_id, query=query)
    except GoogleAdsException:
        return []

    rows = []
    for row in response:
        cost = row.metrics.cost_micros / 1_000_000
        conv_value = row.metrics.conversions_value
        pno = (cost / conv_value * 100) if conv_value > 0 else None
        rows.append({
            "campaign": row.campaign.name,
            "campaign_id": str(row.campaign.id),
            "campaign_budget_resource": row.campaign.campaign_budget,
            "budget_amount": row.campaign_budget.amount_micros / 1_000_000 if row.campaign_budget.amount_micros else None,
            "date": row.segments.date,
            "cost": cost,
            "ctr": row.metrics.ctr * 100,
            "pno": pno,
            "conv_value": conv_value,
            "cpc": row.metrics.average_cpc / 1_000_000,
            "clicks": row.metrics.clicks,
            "impressions": row.metrics.impressions,
        })
    return rows


def fetch_change_events(client, customer_id, days=7):
    """Vrátí dict {campaign_id: [seznam změn]} za posledních N dní."""
    service = client.get_service("GoogleAdsService")
    today = date.today()
    date_from = today - timedelta(days=days)
    date_from_str = date_from.strftime("%Y-%m-%d")
    today_str = today.strftime("%Y-%m-%d")

    query = f"""
        SELECT
            change_event.change_date_time,
            change_event.campaign,
            change_event.change_resource_type,
            change_event.resource_change_operation,
            change_event.user_email,
            change_event.changed_fields,
            change_event.resource_name
        FROM change_event
        WHERE change_event.change_date_time >= '{date_from_str} 00:00:00'
          AND change_event.change_date_time <= '{today_str} 23:59:59'
        ORDER BY change_event.change_date_time DESC
        LIMIT 200
    """
    try:
        response = service.search(customer_id=customer_id, query=query)
    except GoogleAdsException:
        return {}

    changes_by_campaign = {}
    for row in response:
        campaign_resource = row.change_event.campaign
        if not campaign_resource:
            continue
        campaign_id = campaign_resource.split("/")[-1]

        change_dt = row.change_event.change_date_time
        resource_type = row.change_event.change_resource_type.name if row.change_event.change_resource_type else ""
        operation = row.change_event.resource_change_operation.name if row.change_event.resource_change_operation else ""
        user_email = row.change_event.user_email or "neznámý uživatel"
        changed_fields = row.change_event.changed_fields or ""

        resource_name = row.change_event.resource_name or ""

        changes_by_campaign.setdefault(campaign_id, []).append({
            "datetime": change_dt,
            "resource_type": resource_type,
            "operation": operation,
            "user_email": user_email,
            "changed_fields": str(changed_fields),
            "resource_name": resource_name,
            "old_budget": None,
            "new_budget": None,
        })

    return changes_by_campaign


def format_change_summary(changes):
    """Shrne seznam změn do čitelného textu."""
    if not changes:
        return None
    latest = changes[:3]
    lines = []
    for c in latest:
        dt = c["datetime"][:16].replace("T", " ") if "T" in str(c["datetime"]) else str(c["datetime"])[:16]
        rtype = c["resource_type"].replace("_", " ").title()
        op = {"CREATE": "vytvořeno", "UPDATE": "upraveno", "REMOVE": "odstraněno"}.get(c["operation"], c["operation"])
        user = c["user_email"].split("@")[0] if "@" in c["user_email"] else c["user_email"]
        detail = ""
        if c.get("resource_type") == "CAMPAIGN_BUDGET" and c.get("current_budget"):
            detail = f" (aktuálně: {c['current_budget']:,.0f} Kč/den)"
        lines.append(f"{dt} — {rtype} {op}{detail} ({user})")
    return lines


def detect_anomalies(rows, account_name, changes_by_campaign, mc_data=None):
    yesterday = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")

    by_campaign = {}
    for row in rows:
        key = row["campaign"]
        by_campaign.setdefault(key, []).append(row)

    anomalies = []
    metrics_config = [
        ("cost",      "Cena",             "Kč"),
        ("ctr",       "CTR",              "%"),
        ("pno",       "PNO",              "%"),
        ("conv_value","Hodnota konverze", "Kč"),
        ("cpc",       "CPC",              "Kč"),
    ]

    for campaign, data in by_campaign.items():
        yesterday_row = next((r for r in data if r["date"] == yesterday), None)
        if not yesterday_row:
            continue
        baseline = [r for r in data if r["date"] != yesterday]
        if len(baseline) < 3:
            continue

        campaign_id = yesterday_row.get("campaign_id", "")
        campaign_changes = changes_by_campaign.get(campaign_id, [])

        for metric_key, metric_label, unit in metrics_config:
            yesterday_val = yesterday_row.get(metric_key)
            baseline_vals = [r[metric_key] for r in baseline if r.get(metric_key) is not None]
            if yesterday_val is None or not baseline_vals:
                continue

            # PNO: vynechat dny kde je PNO 0 (žádné výdaje) — zkreslují průměr
            if metric_key == "pno":
                baseline_vals = [v for v in baseline_vals if v > 0]
                if len(baseline_vals) < 3:
                    continue

            avg = sum(baseline_vals) / len(baseline_vals)
            if avg == 0:
                continue

            # PNO filter: ignoruj pokud je PNO pod 10 % v celém sledovaném období
            if metric_key == "pno" and avg < 10 and yesterday_val < 10:
                continue

            delta = (yesterday_val - avg) / avg
            if abs(delta) < ANOMALY_THRESHOLD:
                continue

            higher_is_good = METRIC_HIGHER_IS_GOOD.get(metric_key)
            if higher_is_good is None:
                is_good = None
            elif higher_is_good:
                is_good = delta > 0
            else:
                is_good = delta < 0

            # Seřazená časová řada pro sparkline
            all_data = sorted(data, key=lambda r: r["date"])
            daily_labels = [r["date"][5:] for r in all_data]  # MM-DD
            daily_values = [round(r.get(metric_key) or 0, 2) for r in all_data]

            # Počet po sobě jdoucích dní anomálie (zpětně od včerejška)
            consecutive_days = 0
            for row in sorted(data, key=lambda r: r["date"], reverse=True):
                val = row.get(metric_key)
                if val is None:
                    break
                day_delta = (val - avg) / avg
                if abs(day_delta) >= ANOMALY_THRESHOLD and (day_delta > 0) == (delta > 0):
                    consecutive_days += 1
                else:
                    break

            # Pro PNO: spočítej co se změnilo — náklady nebo konverze
            extra_context = {}
            if metric_key == "pno":
                for ctx_key in ("cost", "conv_value"):
                    ctx_yesterday = yesterday_row.get(ctx_key)
                    ctx_baseline = [r[ctx_key] for r in baseline if r.get(ctx_key) is not None]
                    if ctx_yesterday is not None and ctx_baseline:
                        ctx_avg = sum(ctx_baseline) / len(ctx_baseline)
                        if ctx_avg > 0:
                            extra_context[ctx_key] = (ctx_yesterday - ctx_avg) / ctx_avg

            explanation = classify_anomaly(metric_key, delta, yesterday_val, avg, extra_context)

            # Přidej info o manuálních změnách přímo do textu
            current_budget = yesterday_row.get("budget_amount")
            budget_resource = yesterday_row.get("campaign_budget_resource", "")
            enriched_changes = []
            for ch in campaign_changes:
                ch = dict(ch)
                if ch.get("resource_type") == "CAMPAIGN_BUDGET" and current_budget:
                    ch["current_budget"] = current_budget
                enriched_changes.append(ch)

            if enriched_changes:
                n = len(enriched_changes)
                last_change = enriched_changes[0]
                dt = str(last_change["datetime"])[:10]
                explanation += f" Agentura provedla {n} {'změnu' if n == 1 else 'změny' if n < 5 else 'změn'} v kampani (naposledy {dt}) — anomálie může být důsledkem těchto úprav."
            else:
                explanation += " V kampani nebyly za posledních 7 dní zaznamenány žádné manuální změny agentury."
            campaign_changes = enriched_changes

            anomalies.append({
                "account": account_name,
                "campaign": campaign,
                "metric": metric_label,
                "metric_key": metric_key,
                "unit": unit,
                "yesterday": yesterday_val,
                "avg": avg,
                "delta": delta,
                "severity": abs(delta),
                "is_good": is_good,
                "explanation": explanation,
                "has_agency_changes": bool(campaign_changes),
                "change_summary": format_change_summary(campaign_changes) or [],
                "daily_labels": daily_labels,
                "daily_values": daily_values,
                "consecutive_days": consecutive_days,
            })

    anomalies.sort(key=lambda x: x["severity"], reverse=True)
    return anomalies


def classify_anomaly(metric_key, delta, value, avg, extra_context=None):
    yesterday_dow = (date.today() - timedelta(days=1)).weekday()
    extra_context = extra_context or {}

    if yesterday_dow in (5, 6):
        return "Sezónnost — víkend má typicky jiný výkon než pracovní dny."
    if yesterday_dow == 0:
        return "Sezónnost — pondělí se liší od víkendového průměru."

    if metric_key == "pno":
        cost_d = extra_context.get("cost", 0)
        conv_d = extra_context.get("conv_value", 0)
        pno_dir = "zhoršilo" if delta > 0 else "zlepšilo"
        parts = []
        if cost_d > 0.10:
            parts.append(f"náklady vzrostly o {cost_d*100:.0f} %")
        elif cost_d < -0.10:
            parts.append(f"náklady klesly o {abs(cost_d)*100:.0f} %")
        if conv_d < -0.10:
            parts.append(f"hodnota konverzí klesla o {abs(conv_d)*100:.0f} %")
        elif conv_d > 0.10:
            parts.append(f"hodnota konverzí vzrostla o {conv_d*100:.0f} %")
        if parts:
            return f"PNO se {pno_dir} — {' a '.join(parts)}."
        return f"PNO se {pno_dir} — změna je malá u nákladů i konverzí, zkontroluj detaily."

    if metric_key == "conv_value" and delta < -0.4:
        return "Možná chyba trackingu — hodnota konverzí výrazně klesla. Zkontroluj GA4 a tag."
    if metric_key == "ctr" and delta < -0.3:
        return "CTR výrazně kleslo — zkontroluj kvalitu reklam nebo změnu pozice."
    if metric_key == "ctr" and delta > 0.3:
        return "CTR výrazně vzrostlo — kampaň se zlepšila nebo se změnila konkurence v aukci."
    if metric_key == "cost" and delta > 0.5:
        return "Výdaje výrazně vzrostly — zkontroluj bid strategii nebo nové kampaně."
    if metric_key == "cost" and delta < -0.3:
        return "Výdaje výrazně klesly — zkontroluj omezení rozpočtu nebo pauzy v kampani."
    if metric_key == "cpc" and delta > 0.3:
        return "CPC vzrostlo — možná zvýšená konkurence v aukci nebo změna bidů."
    if metric_key == "cpc" and delta < -0.3:
        return "CPC kleslo — kampaň nakupuje levněji, může být příležitost zvýšit rozpočet."
    if metric_key == "conv_value" and delta > 0.3:
        return "Hodnota konverzí vzrostla — kampaň performuje nadprůměrně."
    if delta > 0:
        return "Výkon vzrostl oproti průměru."
    return "Výkon klesl oproti průměru. Zkontroluj kampaň."


def fmt(value, unit):
    if value is None:
        return "N/A"
    if unit == "Kč":
        return f"{value:,.0f} Kč"
    if unit == "%":
        return f"{value:.2f} %"
    return f"{value:.2f}"


def card_colors(is_good):
    if is_good is True:
        return "#e8f5e0", "#7dba68"   # pastelová zelená
    if is_good is False:
        return "#fde8e8", "#e87878"   # pastelová lososová
    return "#fef5e0", "#d4a84b"       # pastelová amber (neutrální)


_card_counter = 0

def make_card(a, rank):
    global _card_counter
    _card_counter += 1
    card_id = f"chart_{_card_counter}"

    bg, accent = card_colors(a["is_good"])
    arrow = "▲" if a["delta"] > 0 else "▼"
    pct = abs(a["delta"]) * 100

    changes_html = ""
    if a["has_agency_changes"]:
        items = "".join(f"<li>{c}</li>" for c in a["change_summary"])
        changes_html = f"""
        <div class="changes-box">
            <strong>Změny agentury (posledních 7 dní):</strong>
            <ul>{items}</ul>
            <em>Anomálie může souviset s těmito změnami.</em>
        </div>"""

    explanation_text = a["explanation"].split(" | Agentura")[0]

    labels = a["daily_labels"]
    values = a["daily_values"]
    # poslední bod = včerejšek, označíme ho jinak
    point_colors = [accent] * len(values)
    if point_colors:
        point_colors[-1] = accent

    sparkline = f"""
    <canvas id="{card_id}" height="55" style="width:100%;margin:8px 0 4px;"></canvas>
    <script>
    (function(){{
      var ctx = document.getElementById('{card_id}').getContext('2d');
      new Chart(ctx, {{
        type: 'line',
        data: {{
          labels: {labels},
          datasets: [{{
            data: {values},
            borderColor: '{accent}',
            backgroundColor: '{accent}22',
            borderWidth: 2,
            pointRadius: 3,
            pointHoverRadius: 6,
            pointBackgroundColor: '{accent}',
            fill: true,
            tension: 0.3
          }}]
        }},
        options: {{
          interaction: {{ mode: 'index', intersect: false }},
          plugins: {{ legend: {{ display: false }}, tooltip: {{ callbacks: {{
            label: function(c) {{ return c.parsed.y.toFixed(2) + ' {a["unit"]}'; }}
          }} }} }},
          scales: {{
            x: {{ grid: {{ display: false }}, ticks: {{ font: {{ size: 9 }}, color: '#94a3b8' }} }},
            y: {{ grid: {{ color: '#e2e8f022' }}, ticks: {{ font: {{ size: 9 }}, color: '#94a3b8' }} }}
          }}
        }}
      }});
    }})();
    </script>"""

    owner = a.get("owner", "Ostatní")
    days = a.get("consecutive_days", 1)
    if days >= 3:
        days_badge = f'<span class="days-badge days-hot">{days} dny v anomálii</span>'
    elif days == 2:
        days_badge = f'<span class="days-badge days-warn">2 dny v anomálii</span>'
    else:
        days_badge = f'<span class="days-badge days-ok">1. den</span>'

    is_good_val = "1" if a["is_good"] is True else "0"
    days_cat = "3+" if days >= 3 else str(days)

    return f"""
    <div class="card" data-owner="{owner}" data-good="{is_good_val}" data-metric="{a['metric']}" data-days="{days_cat}" style="border-left: 5px solid {accent}; background: {bg};">
        <div class="card-header-row">
          <div class="card-rank" style="color:{accent}">#{rank}</div>
          {days_badge}
        </div>
        <div class="card-account">{a['account']}</div>
        <div class="card-campaign">{a['campaign']}</div>
        <div class="card-metric" style="color:{accent}">{a['metric']} {arrow} {pct:.0f}%</div>
        <div class="card-values">
            Včera: <strong>{fmt(a['yesterday'], a['unit'])}</strong>
            &nbsp;|&nbsp;
            7d průměr: <strong>{fmt(a['avg'], a['unit'])}</strong>
        </div>
        {sparkline}
        <div class="card-explanation">💡 {explanation_text}</div>
        {changes_html}
    </div>
    """


# ── Merchant Center helpers ──────────────────────────────────────────────────

def get_mc_credentials():
    """Returns refreshed google.oauth2.credentials.Credentials using .env values."""
    creds = google.oauth2.credentials.Credentials(
        token=None,
        refresh_token=REFRESH_TOKEN,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        token_uri="https://oauth2.googleapis.com/token",
    )
    request = google.auth.transport.requests.Request()
    creds.refresh(request)
    return creds


def fetch_mc_disapprovals(mc_id, creds):
    """Counts disapproved and pending products for Shopping destination, aggregates disapproval reasons."""
    try:
        total = 0
        disapproved = 0
        pending = 0
        reasons = {}  # description -> count
        page_token = None
        base_url = f"https://shoppingcontent.googleapis.com/content/v2.1/{mc_id}/productstatuses?maxResults=250"

        while True:
            url = base_url
            if page_token:
                url += f"&pageToken={page_token}"

            req = urllib.request.Request(url)
            req.add_header("Authorization", f"Bearer {creds.token}")

            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())

            resources = data.get("resources", [])
            for row in resources:
                total += 1
                is_disapproved = False
                is_pending = False
                for ds in row.get("destinationStatuses", []):
                    if ds.get("destination") == "Shopping":
                        if ds.get("disapprovedCountries"):
                            is_disapproved = True
                        if ds.get("pendingCountries"):
                            is_pending = True
                        break
                if is_disapproved:
                    disapproved += 1
                    for issue in row.get("itemLevelIssues", []):
                        if issue.get("destination") == "Shopping" and issue.get("servability") == "disapproved":
                            desc = issue.get("description", issue.get("code", "Neznámý důvod"))
                            reasons[desc] = reasons.get(desc, 0) + 1
                elif is_pending:
                    pending += 1

            page_token = data.get("nextPageToken")
            if not page_token:
                break

        top_reasons = sorted(reasons.items(), key=lambda x: x[1], reverse=True)[:5]
        return {"total": total, "disapproved": disapproved, "pending": pending, "reasons": top_reasons}
    except Exception as e:
        print(f"    MC fetch error for {mc_id}: {e}")
        return {"total": 0, "disapproved": 0, "pending": 0, "reasons": []}


def load_mc_history():
    """Reads mc_history.json from script directory, returns dict."""
    if not os.path.exists(MC_HISTORY_FILE):
        return {}
    try:
        with open(MC_HISTORY_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_mc_history(history):
    """Saves dict to mc_history.json."""
    with open(MC_HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def fetch_all_mc_data(creds, accounts_with_mc):
    """
    Fetches disapprovals for all accounts (skipping placeholder "000000000"),
    updates history with today's date, saves history.
    Returns current day data dict: {gads_id: {"mc_id": str, "disapproved": int, "total": int, "name": str}}
    """
    today_str = date.today().strftime("%Y-%m-%d")
    history = load_mc_history()
    current = {}

    account_owners = load_account_owners()

    # Build a name lookup from account owners file — we'll populate names from
    # the accounts list passed via MC_ACCOUNT_MAP keys; fallback to gads_id
    for gads_id, mc_id in accounts_with_mc.items():
        if mc_id == "000000000":
            continue
        print(f"  Stahuji MC data: {gads_id} (MC: {mc_id})...")
        result = fetch_mc_disapprovals(mc_id, creds)

        entry = {
            "mc_id": mc_id,
            "disapproved": result["disapproved"],
            "pending": result.get("pending", 0),
            "total": result["total"],
            "reasons": result.get("reasons", []),
            "name": gads_id,  # will be enriched by caller if needed
        }
        current[gads_id] = entry

        # Update history: history[gads_id][date] = disapproved count
        if gads_id not in history:
            history[gads_id] = {}
        history[gads_id][today_str] = result["disapproved"]

    save_mc_history(history)
    return current


def make_mc_tab_html(mc_data, account_owners, mc_history):
    """Generates the HTML content for the Merchant Center tab."""
    if not mc_data:
        return '<p style="color:#94a3b8;text-align:center;padding:40px 0">Žádná MC data k dispozici.</p>'

    today_str = date.today().strftime("%Y-%m-%d")
    # Build last 14 days label list
    last_14 = [(date.today() - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(13, -1, -1)]
    last_14_labels = [(date.today() - timedelta(days=i)).strftime("%m-%d") for i in range(13, -1, -1)]

    # Sort by disapproved desc
    sorted_items = sorted(mc_data.items(), key=lambda x: x[1].get("disapproved", 0), reverse=True)

    rows_html = []
    mc_chart_counter = 0
    for gads_id, info in sorted_items:
        mc_chart_counter += 1
        chart_id = f"mc_chart_{mc_chart_counter}"

        name = info.get("name", gads_id)
        disapproved = info.get("disapproved", 0)
        pending = info.get("pending", 0)
        total = info.get("total", 0)
        owner = account_owners.get(gads_id, "Ostatní")

        pct_str = f"{disapproved/total*100:.1f} %" if total > 0 else "—"

        # Badge color for disapproved count
        if disapproved > 0:
            badge_bg = "#fde8e8"
            badge_color = "#e87878"
        else:
            badge_bg = "#e8f5e0"
            badge_color = "#7dba68"

        # Owner badge color
        owner_colors = {
            "Veronika": ("#d8eeec", "#6abdb5"),  # mint/tyrkys
            "Sabina": ("#fde8ef", "#f0a8c0"),    # světle růžová
        }
        ob_bg, ob_color = owner_colors.get(owner, ("#ece8f8", "#a898d0"))  # levandule pro Ostatní

        # Sparkline data from history
        acct_history = mc_history.get(gads_id, {})
        spark_values = [acct_history.get(d, 0) for d in last_14]
        # override today's value with fresh data
        spark_values[-1] = disapproved

        spark_js = f"""
        (function(){{
          var ctx = document.getElementById('{chart_id}').getContext('2d');
          new Chart(ctx, {{
            type: 'line',
            data: {{
              labels: {last_14_labels},
              datasets: [{{
                data: {spark_values},
                borderColor: '#e87878',
                backgroundColor: '#e8787822',
                borderWidth: 1.5,
                pointRadius: 2,
                pointHoverRadius: 4,
                fill: true,
                tension: 0.3
              }}]
            }},
            options: {{
              plugins: {{ legend: {{ display: false }}, tooltip: {{ callbacks: {{
                label: function(c) {{ return c.parsed.y + ' zamítnutých'; }}
              }} }} }},
              scales: {{
                x: {{ grid: {{ display: false }}, ticks: {{ font: {{ size: 8 }}, color: '#94a3b8', maxTicksLimit: 7 }} }},
                y: {{ grid: {{ color: '#e2e8f022' }}, ticks: {{ font: {{ size: 8 }}, color: '#94a3b8', precision: 0 }}, min: 0 }}
              }}
            }}
          }});
        }})();"""

        reasons = info.get("reasons", [])
        reasons_html = ""
        if disapproved > 0 and reasons:
            reason_items = "".join(
                f'<li style="display:flex;justify-content:space-between;gap:8px;padding:3px 0;border-bottom:1px solid #f1f5f9">'
                f'<span style="color:#475569">{desc}</span>'
                f'<span style="font-weight:600;color:#e87878;white-space:nowrap">{cnt}×</span></li>'
                for desc, cnt in reasons
            )
            reasons_html = f"""
          <div style="margin-top:10px">
            <div style="font-size:11px;font-weight:600;color:#94a3b8;text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px">Důvody zamítnutí</div>
            <ul style="list-style:none;margin:0;padding:0;font-size:12px">{reason_items}</ul>
          </div>"""

        rows_html.append(f"""
        <div class="mc-card" data-mc-owner="{owner}" data-has-disapproved="{'true' if disapproved > 0 else 'false'}">
          <div class="mc-card-top">
            <div>
              <div class="mc-account-name">{name}</div>
              <span class="mc-owner-badge" style="background:{ob_bg};color:{ob_color}">{owner}</span>
            </div>
            <div class="mc-stats">
              <span class="mc-disapproved-badge" style="background:{badge_bg};color:{badge_color}">{disapproved} neschválených</span>
              {'<span style="background:#fef5e0;color:#d4a84b;font-size:11px;padding:2px 7px;border-radius:99px;font-weight:600">' + str(pending) + ' probíhá kontrola</span>' if pending > 0 else ''}
              <span class="mc-total">{total} celkem</span>
              <span class="mc-pct">{pct_str}</span>
            </div>
          </div>
          <canvas id="{chart_id}" height="50" style="width:100%;margin-top:8px;"></canvas>
          <script>{spark_js}</script>
          {reasons_html}
        </div>""")

    return "\n".join(rows_html)


def generate_html(all_anomalies, yesterday_str, mc_data=None, account_owners=None):
    global _card_counter
    _card_counter = 0
    pw_hash = PW_HASH

    bad  = [a for a in all_anomalies if a["is_good"] is False]
    good = [a for a in all_anomalies if a["is_good"] is True]

    # globální pořadí pro každou anomálii
    rank_map = {id(a): i + 1 for i, a in enumerate(all_anomalies)}

    def col(items):
        if not items:
            return '<p class="empty-col">Žádné anomálie v této kategorii.</p>'
        return "".join(make_card(a, rank_map[id(a)]) for a in items)

    bad_html  = col(bad)
    good_html = col(good)

    total = len(all_anomalies)

    # MC tab content
    mc_history = load_mc_history()
    owners = account_owners or {}

    # Enrich mc_data names from account owners file (owners keys are gads_ids)
    # We need account names — they come via the accounts list in main(); use a
    # reverse lookup from owners or fall back to gads_id
    mc_tab_content = make_mc_tab_html(mc_data or {}, owners, mc_history)

    # Count MC accounts with disapprovals for subtitle
    mc_disapproved_count = sum(1 for v in (mc_data or {}).values() if v.get("disapproved", 0) > 0)

    html = f"""<!DOCTYPE html>
<html lang="cs">
<head>
<meta charset="UTF-8">
<title>Google Ads Anomaly Radar — {yesterday_str}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #f3f4f6; margin: 0; padding: 24px; color: #3a4555; }}
  .header {{ margin-bottom: 16px; }}
  h1 {{ font-size: 22px; font-weight: 700; margin: 0 0 4px; }}
  .subtitle {{ color: #64748b; font-size: 13px; }}

  /* Tab navigation */
  .tab-nav {{ display: flex; gap: 4px; margin-bottom: 20px; border-bottom: 2px solid #e2e8f0; padding-bottom: 0; }}
  .tab-btn {{ padding: 10px 22px; border: none; background: white; cursor: pointer;
              font-size: 14px; font-weight: 600; color: #64748b;
              border-radius: 8px 8px 0 0; border: 1.5px solid #e2e8f0;
              border-bottom: none; margin-bottom: -2px; transition: all 0.15s; }}
  .tab-btn:hover {{ background: #f8fafc; color: #3a4555; }}
  .tab-btn.active {{ background: #b4a0d0; color: white; border-color: #b4a0d0; }}
  .tab-content {{ display: none; }}
  .tab-content.active {{ display: block; }}

  .cols {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; align-items: start; }}
  .col-header {{ font-size: 13px; font-weight: 700; text-transform: uppercase;
                 letter-spacing: 0.5px; padding: 8px 12px; border-radius: 8px;
                 margin-bottom: 12px; }}
  .col-bad  .col-header {{ background: #fde8e8; color: #e87878; }}
  .col-good .col-header {{ background: #e8f5e0; color: #7dba68; }}
  .card {{ border-radius: 10px; padding: 16px 18px; margin-bottom: 10px;
           box-shadow: 0 1px 3px rgba(0,0,0,0.07); }}
  .card-rank {{ font-size: 10px; font-weight: 700; text-transform: uppercase;
                letter-spacing: 1px; margin-bottom: 3px; }}
  .card-account {{ font-size: 11px; color: #64748b; margin-bottom: 2px; }}
  .card-campaign {{ font-size: 14px; font-weight: 600; margin-bottom: 5px;
                    line-height: 1.3; }}
  .card-metric {{ font-size: 18px; font-weight: 700; margin-bottom: 5px; }}
  .card-values {{ font-size: 12px; color: #475569; margin-bottom: 7px; }}
  .card-explanation {{ font-size: 12px; background: rgba(255,255,255,0.65);
                        border-radius: 6px; padding: 8px 12px; line-height: 1.5; }}
  .changes-box {{ margin-top: 8px; font-size: 11px; background: rgba(255,255,255,0.7);
                  border-radius: 6px; padding: 8px 12px; line-height: 1.6; }}
  .changes-box ul {{ margin: 3px 0 4px 14px; padding: 0; }}
  .changes-box em {{ color: #64748b; }}
  .empty-col {{ color: #94a3b8; font-size: 13px; padding: 16px 0; text-align: center; }}
  .footer {{ margin-top: 28px; font-size: 11px; color: #94a3b8; text-align: center; }}
  .filters {{ margin-top: 12px; display: flex; gap: 8px; flex-wrap: wrap; }}
  .filter-btn {{ padding: 6px 16px; border-radius: 20px; border: 1.5px solid #cbd5e1;
                 background: white; cursor: pointer; font-size: 13px; font-weight: 500;
                 color: #475569; transition: all 0.15s; }}
  .filter-btn:hover {{ border-color: #94a3b8; background: #f8fafc; }}
  .filter-btn.active {{ background: #b4a0d0; color: white; border-color: #b4a0d0; }}
  .filter-btn.active[data-person="Veronika"] {{ background: #6abdb5; border-color: #6abdb5; }}
  .filter-btn.active[data-person="Sabina"]   {{ background: #f0a8c0; border-color: #f0a8c0; }}
  .filter-btn.active[data-person="Ostatní"]  {{ background: #b4a0d0; border-color: #b4a0d0; }}
  .card-header-row {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 3px; }}
  .days-badge {{ font-size: 10px; font-weight: 600; padding: 2px 8px; border-radius: 10px; }}
  .days-ok   {{ background: #eaecf0; color: #7a8899; }}
  .days-warn {{ background: #fef5e0; color: #d4a84b; }}
  .days-hot  {{ background: #fde8e8; color: #e87878; border: 1.5px solid white; }}
  .filter-row {{ display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }}
  .filter-label {{ font-size: 11px; color: #94a3b8; font-weight: 600;
                   text-transform: uppercase; letter-spacing: 0.5px; margin-right: 2px; }}
  .sub-filters {{ margin-top: 8px; padding-top: 8px; border-top: 1px solid #e2e8f0;
                  display: flex; gap: 16px; flex-wrap: wrap; }}
  #pw-gate {{ position:fixed; inset:0; background:#f3f4f6; z-index:9999;
             display:flex; align-items:center; justify-content:center; }}
  #pw-box  {{ background:white; border-radius:16px; padding:40px 36px;
             box-shadow:0 4px 24px rgba(0,0,0,0.12); text-align:center;
             max-width:360px; width:90%; }}
  #pw-input {{ width:100%; padding:10px 14px; border:1.5px solid #cbd5e1;
              border-radius:8px; font-size:15px; margin-bottom:10px;
              outline:none; font-family:inherit; display:block; }}
  #pw-btn   {{ width:100%; padding:10px; background:#b4a0d0; color:white;
              border:none; border-radius:8px; font-size:15px; cursor:pointer;
              font-weight:600; }}
  #pw-btn:hover {{ background:#a490c0; }}

  /* MC tab styles */
  .mc-filters {{ margin-bottom: 16px; display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }}
  .mc-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 14px; }}
  .mc-card {{ background: white; border-radius: 10px; padding: 16px 18px;
              box-shadow: 0 1px 3px rgba(0,0,0,0.07); border-left: 4px solid #e2e8f0; }}
  .mc-card[data-has-disapproved="true"] {{ border-left-color: #e87878; }}
  .mc-card-top {{ display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }}
  .mc-account-name {{ font-size: 14px; font-weight: 600; margin-bottom: 4px; }}
  .mc-owner-badge {{ font-size: 10px; font-weight: 600; padding: 2px 8px;
                     border-radius: 10px; display: inline-block; }}
  .mc-stats {{ display: flex; flex-direction: column; align-items: flex-end; gap: 3px; }}
  .mc-disapproved-badge {{ font-size: 12px; font-weight: 700; padding: 3px 10px;
                            border-radius: 12px; white-space: nowrap; }}
  .mc-total {{ font-size: 11px; color: #64748b; }}
  .mc-pct {{ font-size: 11px; color: #94a3b8; }}
</style>
<script>
  var activeOwner  = 'Všichni';
  var activeMetric = 'Vše';
  var activeDays   = 'Vše';
  var activeMcOwner = 'Všichni';

  function applyFilters() {{
    var bad = 0, good = 0;
    document.querySelectorAll('.card').forEach(function(card) {{
      var show = true;
      if (activeOwner  !== 'Všichni' && !card.dataset.owner.includes(activeOwner))  show = false;
      if (activeMetric !== 'Vše'     && card.dataset.metric !== activeMetric) show = false;
      if (activeDays   !== 'Vše'     && card.dataset.days   !== activeDays)   show = false;
      card.style.display = show ? '' : 'none';
      if (show) {{ card.dataset.good === '1' ? good++ : bad++; }}
    }});
    document.getElementById('count-bad').textContent      = bad  + ' problémů';
    document.getElementById('count-good').textContent     = good + ' zlepšení';
    document.getElementById('col-header-bad').textContent  = 'Problémy — ' + bad;
    document.getElementById('col-header-good').textContent = 'Zlepšení — ' + good;
  }}

  function setOwner(val, btn) {{
    activeOwner = val;
    document.querySelectorAll('.btn-owner').forEach(function(b) {{ b.classList.remove('active'); }});
    btn.classList.add('active');
    applyFilters();
  }}

  function setMetric(val, btn) {{
    activeMetric = val;
    document.querySelectorAll('.btn-metric').forEach(function(b) {{ b.classList.remove('active'); }});
    btn.classList.add('active');
    applyFilters();
  }}

  function setDays(val, btn) {{
    activeDays = val;
    document.querySelectorAll('.btn-days').forEach(function(b) {{ b.classList.remove('active'); }});
    btn.classList.add('active');
    applyFilters();
  }}

  function applyMcFilters() {{
    document.querySelectorAll('.mc-card').forEach(function(card) {{
      var show = true;
      if (activeMcOwner !== 'Všichni' && !card.dataset.mcOwner.includes(activeMcOwner)) show = false;
      card.style.display = show ? '' : 'none';
    }});
  }}

  function setMcOwner(val, btn) {{
    activeMcOwner = val;
    document.querySelectorAll('.btn-mc-owner').forEach(function(b) {{ b.classList.remove('active'); }});
    btn.classList.add('active');
    applyMcFilters();
  }}

  function switchTab(tabId, btn) {{
    document.querySelectorAll('.tab-content').forEach(function(t) {{ t.classList.remove('active'); }});
    document.querySelectorAll('.tab-btn').forEach(function(b) {{ b.classList.remove('active'); }});
    document.getElementById(tabId).classList.add('active');
    btn.classList.add('active');
  }}

  var PW_HASH = "{pw_hash}";
  async function _hashPw(s) {{
    var buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(s));
    return Array.from(new Uint8Array(buf)).map(function(b) {{ return b.toString(16).padStart(2,'0'); }}).join('');
  }}
  async function checkPassword() {{
    var h = await _hashPw(document.getElementById('pw-input').value);
    if (h === PW_HASH) {{
      localStorage.setItem('radar_pw_hash', h);
      document.getElementById('pw-gate').style.display = 'none';
    }} else {{
      document.getElementById('pw-error').style.display = 'block';
    }}
  }}
  window.addEventListener('DOMContentLoaded', async function() {{
    if (!PW_HASH) {{ document.getElementById('pw-gate').style.display = 'none'; return; }}
    if (localStorage.getItem('radar_pw_hash') === PW_HASH)
      document.getElementById('pw-gate').style.display = 'none';
  }});
</script>
</head>
<body>
  <div id="pw-gate">
    <div id="pw-box">
      <div style="font-size:36px;margin-bottom:12px">&#128225;</div>
      <h2 style="margin:0 0 4px;font-size:20px;font-weight:700">Anomaly Radar</h2>
      <p style="color:#64748b;font-size:13px;margin:0 0 20px">Zadej heslo pro přístup k reportu</p>
      <input type="password" id="pw-input" placeholder="Heslo"
        onkeydown="if(event.key==='Enter')checkPassword()">
      <button id="pw-btn" onclick="checkPassword()">Vstoupit &rarr;</button>
      <p id="pw-error" style="color:#e87878;font-size:13px;margin:10px 0 0;display:none">Špatné heslo, zkus znovu.</p>
    </div>
  </div>
  <div class="header">
    <h1>Google Ads Anomaly Radar</h1>
    <div class="subtitle">
      Včera ({yesterday_str}) vs. 7denní průměr &nbsp;·&nbsp;
      {total} anomálií celkem &nbsp;·&nbsp;
      <span style="color:#e87878">▼ <span id="count-bad">{len(bad)} problémů</span></span> &nbsp;
      <span style="color:#7dba68">▲ <span id="count-good">{len(good)} zlepšení</span></span>
      &nbsp;·&nbsp;
      <span style="color:#e87878">🛒 {mc_disapproved_count} MC účtů se zamítnutými produkty</span>
    </div>
  </div>

  <!-- Tab navigation -->
  <div class="tab-nav">
    <button class="tab-btn active" onclick="switchTab('tab-anomalie', this)">Anomálie</button>
    <button class="tab-btn" onclick="switchTab('tab-mc', this)">Merchant Center</button>
  </div>

  <!-- Tab 1: Anomálie -->
  <div id="tab-anomalie" class="tab-content active">
    <div class="filters">
      <div class="filter-row">
        <span class="filter-label">Správce:</span>
        <button class="filter-btn btn-owner active" data-person="Všichni" onclick="setOwner('Všichni',this)">Všichni</button>
        <button class="filter-btn btn-owner" data-person="Veronika" onclick="setOwner('Veronika',this)">Veronika</button>
        <button class="filter-btn btn-owner" data-person="Sabina" onclick="setOwner('Sabina',this)">Sabina</button>
        <button class="filter-btn btn-owner" data-person="Ostatní" onclick="setOwner('Ostatní',this)">Ostatní</button>
      </div>
      <div class="sub-filters">
        <div class="filter-row">
          <span class="filter-label">Metrika:</span>
          <button class="filter-btn btn-metric active" onclick="setMetric('Vše',this)">Vše</button>
          <button class="filter-btn btn-metric" onclick="setMetric('PNO',this)">PNO</button>
          <button class="filter-btn btn-metric" onclick="setMetric('CTR',this)">CTR</button>
          <button class="filter-btn btn-metric" onclick="setMetric('Cena',this)">Cena</button>
          <button class="filter-btn btn-metric" onclick="setMetric('Hodnota konverze',this)">Hodnota konverze</button>
          <button class="filter-btn btn-metric" onclick="setMetric('CPC',this)">CPC</button>
        </div>
        <div class="filter-row">
          <span class="filter-label">Dny v anomálii:</span>
          <button class="filter-btn btn-days active" onclick="setDays('Vše',this)">Vše</button>
          <button class="filter-btn btn-days" onclick="setDays('1',this)">1 den</button>
          <button class="filter-btn btn-days" onclick="setDays('2',this)">2 dny</button>
          <button class="filter-btn btn-days" onclick="setDays('3+',this)">3+ dny</button>
        </div>
      </div>
    </div>
    <div class="cols" style="margin-top:16px">
      <div class="col-bad">
        <div class="col-header" id="col-header-bad">Problémy — {len(bad)}</div>
        {bad_html}
      </div>
      <div class="col-good">
        <div class="col-header" id="col-header-good">Zlepšení — {len(good)}</div>
        {good_html}
      </div>
    </div>
  </div>

  <!-- Tab 2: Merchant Center -->
  <div id="tab-mc" class="tab-content">
    <div class="mc-filters">
      <span class="filter-label">Správce:</span>
      <button class="filter-btn btn-mc-owner active" data-person="Všichni" onclick="setMcOwner('Všichni',this)">Všichni</button>
      <button class="filter-btn btn-mc-owner" data-person="Veronika" onclick="setMcOwner('Veronika',this)">Veronika</button>
      <button class="filter-btn btn-mc-owner" data-person="Sabina" onclick="setMcOwner('Sabina',this)">Sabina</button>
      <button class="filter-btn btn-mc-owner" data-person="Ostatní" onclick="setMcOwner('Ostatní',this)">Ostatní</button>
    </div>
    <div class="mc-grid">
      {mc_tab_content}
    </div>
  </div>

  <div class="footer">Vygenerováno automaticky · {date.today().strftime('%d.%m.%Y')}</div>
</body>
</html>"""
    return html


def main():
    yesterday = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
    print(f"Google Ads Anomaly Radar — analýza za {yesterday}")
    print("Připojuji se k Google Ads API...")

    client = get_client()

    print("Stahuji seznam účtů z MCC...")
    accounts = list_child_accounts(client)
    if not accounts:
        print("Žádné aktivní účty nenalezeny pod MCC.")
        sys.exit(1)
    excluded = load_excluded_accounts()
    accounts = [a for a in accounts if a["id"] not in excluded]
    print(f"Nalezeno {len(accounts)} účtů (po vyloučení {len(excluded)} z excluded_accounts.txt).")

    account_owners = load_account_owners()

    all_anomalies = []
    # Build name lookup for accounts
    account_names = {acc["id"]: acc["name"] for acc in accounts}

    for acc in accounts:
        print(f"  Stahuji data: {acc['name']} ({acc['id']})...")
        rows = fetch_campaign_metrics(client, acc["id"], days=7)
        changes = fetch_change_events(client, acc["id"], days=7)
        if rows:
            anomalies = detect_anomalies(rows, acc["name"], changes)
            owner = account_owners.get(acc["id"], "Ostatní")
            for a in anomalies:
                a["owner"] = owner
            all_anomalies.extend(anomalies)

    all_anomalies.sort(key=lambda x: x["severity"], reverse=True)

    print(f"\nNalezeno {len(all_anomalies)} anomálií celkem.")

    # Fetch Merchant Center data
    print("Stahuji data z Merchant Center...")
    try:
        creds = get_mc_credentials()
        mc_data = fetch_all_mc_data(creds, MC_ACCOUNT_MAP)
        # Enrich mc_data with account names
        for gads_id, info in mc_data.items():
            if gads_id in account_names:
                info["name"] = account_names[gads_id]
        print(f"MC data stažena pro {len(mc_data)} účtů.")
    except Exception as e:
        print(f"MC fetch selhal: {e}")
        mc_data = {}

    print("Generuji HTML report...")

    html = generate_html(all_anomalies, yesterday, mc_data=mc_data, account_owners=account_owners)
    output_path = os.path.join(os.path.dirname(__file__), "report.html")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"Report uložen: {output_path}")
    if not os.getenv("CI"):
        webbrowser.open(f"file:///{output_path}")

    if GITHUB_TOKEN and GITHUB_REPO:
        print("Nahrávám report na GitHub Pages...")
        url = deploy_to_github(html)
        if url:
            print(f"Report online: {url}")
        else:
            print("GitHub upload selhal — report je stále dostupný lokálně.")
    print("Hotovo!")


def deploy_to_github(html_content):
    """Pushes index.html to GitHub Pages repo via GitHub API."""
    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/index.html"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
        "Content-Type": "application/json",
    }

    # Get current file SHA (needed for updates)
    sha = None
    try:
        req = urllib.request.Request(api_url)
        for k, v in headers.items():
            req.add_header(k, v)
        with urllib.request.urlopen(req, timeout=15) as resp:
            sha = json.loads(resp.read().decode()).get("sha")
    except Exception:
        pass  # soubor ještě neexistuje

    encoded = base64.b64encode(html_content.encode("utf-8")).decode("ascii")
    payload = {"message": f"report {date.today()}", "content": encoded}
    if sha:
        payload["sha"] = sha

    try:
        req2 = urllib.request.Request(
            api_url, data=json.dumps(payload).encode("utf-8"), method="PUT"
        )
        for k, v in headers.items():
            req2.add_header(k, v)
        with urllib.request.urlopen(req2, timeout=60) as resp:
            json.loads(resp.read().decode())
        owner = GITHUB_REPO.split("/")[0].lower()
        repo = GITHUB_REPO.split("/")[1]
        return f"https://{owner}.github.io/{repo}/"
    except urllib.error.HTTPError as e:
        print(f"GitHub HTTP chyba {e.code}: {e.read().decode()[:200]}")
        return None
    except Exception as e:
        print(f"GitHub chyba: {e}")
        return None


if __name__ == "__main__":
    main()
