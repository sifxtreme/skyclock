#!/usr/bin/env python3
"""Sky clock: an 800x480 PNG for the Waveshare 7" LCD.

The background is the real sky colour for the sun's altitude over the house.
A 24-hour band draws the sun's path, the five prayers sit on it, and a dot
marks now. Prayer times follow ISOC (Garden Grove), from its themasjidapp.org
azanParams: Fajr/Isha 15 deg, Shafi Asr, Dhuhr +1 min, Maghrib +3 min.
Times drop the seconds, as ISOC's table does, except Maghrib, which rounds up.

  python3 skyclock.py render out.png [--at 2026-10-13T18:05]
  python3 skyclock.py serve 8000 [host]  # GET / page, /sky.png (fresh each time), /healthz
"""
import io, math, random, sys
from datetime import datetime, timedelta, date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo
from PIL import Image, ImageDraw, ImageFont, ImageFilter

LAT, LNG = 33.84, -117.91              # home, to ~1 km (Anaheim): the sky and sun curve
ISOC_LAT, ISOC_LNG = 33.7555012, -117.9584296  # ISOC masjid: prayer times
ADJUST_MIN = {"Dhuhr": 1, "Maghrib": 3}  # ISOC azanParams.adjustments
TZ = ZoneInfo("America/Los_Angeles")
W, H, S = 800, 480, 2                  # S = supersample factor

ARABIC = "/System/Library/Fonts/Supplemental/Baghdad.ttc"
PRAYERS = [("Fajr", "الفجر"), ("Sunrise", "الشروق"), ("Dhuhr", "الظهر"),
           ("Asr", "العصر"), ("Maghrib", "المغرب"), ("Isha", "العشاء")]
ARABIC_NAMES = dict(PRAYERS, Duha="الضحى")
DUHA_AFTER_SUNRISE_MIN = 15  # ISOC duhaOffset
ENDING_MIN, URGENT_MIN = 20, 5  # current prayer turns amber, then red, this close to its end

# ---------- sun math (praytimes.org formulation) ----------
def _sun(jd):
    d = jd - 2451545.0
    g = math.radians((357.529 + 0.98560028 * d) % 360)
    q = (280.459 + 0.98564736 * d) % 360
    L = math.radians((q + 1.915 * math.sin(g) + 0.020 * math.sin(2 * g)) % 360)
    e = math.radians(23.439 - 0.00000036 * d)
    ra = math.degrees(math.atan2(math.cos(e) * math.sin(L), math.cos(L))) / 15
    decl = math.asin(math.sin(e) * math.sin(L))
    eqt = q / 15 - (ra % 24)
    eqt = (eqt + 12) % 24 - 12
    return decl, eqt

def _jd(d):
    return datetime(d.year, d.month, d.day, 12).toordinal() + 1721424.5

def _utc_offset(d):
    return datetime(d.year, d.month, d.day, 12, tzinfo=TZ).utcoffset().total_seconds() / 3600

def prayer_times(d):
    """Local clock hours for each prayer on date d, on whole minutes."""
    out = {}
    for k, mins in prayer_minutes_exact(d).items():
        # ISOC's published table drops the seconds (floor). Maghrib rounds UP instead,
        # so the clock never shows it before sunset (+3 min) has passed.
        out[k] = (math.ceil(mins - 1e-9) if k == "Maghrib" else math.floor(mins)) / 60
    return out

def prayer_minutes_exact(d):
    """Unrounded local minutes after midnight, ISOC adjustments applied."""
    jd = _jd(d) - ISOC_LNG / 360
    tz = _utc_offset(d)
    lat = math.radians(ISOC_LAT)

    def noon(t):
        _, eqt = _sun(jd + t / 24)
        return 12 - eqt

    def at_angle(angle_deg, t, ccw):
        decl, _ = _sun(jd + t / 24)
        a = math.radians(angle_deg)
        x = (-math.sin(a) - math.sin(lat) * math.sin(decl)) / (math.cos(lat) * math.cos(decl))
        h = math.degrees(math.acos(max(-1, min(1, x)))) / 15
        return noon(t) + (-h if ccw else h)

    def asr(t):
        # Shadow length uses the declination at 0h UT of the date, as adhan.js
        # (ISOC's engine) does; the hour angle below still uses the live value.
        decl, _ = _sun(_jd(d) - 0.5)
        ang = -math.degrees(math.atan(1 / (1 + math.tan(abs(lat - decl)))))
        return at_angle(ang, t, False)

    # two passes, as praytimes does
    t = {"Fajr": 5, "Sunrise": 6, "Dhuhr": 12, "Asr": 13, "Maghrib": 18, "Isha": 18}
    for _ in range(2):
        f = {k: v / 24 for k, v in t.items()}
        t = {"Fajr": at_angle(15, t["Fajr"], True),
             "Sunrise": at_angle(0.833, t["Sunrise"], True),
             "Dhuhr": noon(t["Dhuhr"]),
             "Asr": asr(t["Asr"]),
             "Maghrib": at_angle(0.833, t["Maghrib"], False),
             "Isha": at_angle(15, t["Isha"], False)}
    shift = tz - ISOC_LNG / 15
    return {k: (v + shift) * 60 + ADJUST_MIN.get(k, 0) for k, v in t.items()}

def sun_altitude(dt):
    """Sun altitude in degrees at aware datetime dt."""
    u = dt.astimezone(ZoneInfo("UTC"))
    jd = datetime(u.year, u.month, u.day).toordinal() + 1721424.5 + (u.hour + u.minute / 60) / 24
    decl, eqt = _sun(jd)
    solar = u.hour + u.minute / 60 + u.second / 3600 + LNG / 15 + eqt
    ha = math.radians((solar - 12) * 15)
    lat = math.radians(LAT)
    return math.degrees(math.asin(math.sin(lat) * math.sin(decl) + math.cos(lat) * math.cos(decl) * math.cos(ha)))

def moon_phase(dt):
    """(illuminated fraction 0..1, waxing?) at aware datetime dt.

    Meeus, Astronomical Algorithms ch. 48 (phase angle from the Sun-Moon
    elongation, with the main periodic terms). Good to about 0.1% of the disc.
    """
    u = dt.astimezone(ZoneInfo("UTC"))
    jd = datetime(u.year, u.month, u.day).toordinal() + 1721424.5 + (u.hour + u.minute / 60 + u.second / 3600) / 24
    T = (jd - 2451545.0) / 36525
    D = math.radians((297.8501921 + 445267.1114034 * T - 0.0018819 * T * T) % 360)   # mean elongation
    Ms = math.radians((357.5291092 + 35999.0502909 * T - 0.0001536 * T * T) % 360)   # Sun mean anomaly
    Mm = math.radians((134.9633964 + 477198.8675055 * T + 0.0087414 * T * T) % 360)  # Moon mean anomaly
    i = (180 - math.degrees(D) - 6.289 * math.sin(Mm) + 2.100 * math.sin(Ms) - 1.274 * math.sin(2 * D - Mm)
         - 0.658 * math.sin(2 * D) - 0.214 * math.sin(2 * Mm) - 0.110 * math.sin(D))
    frac = (1 + math.cos(math.radians(i))) / 2
    return frac, math.degrees(D) % 360 < 180

def hijri(d):
    """Umm al-Qura Hijri (what ISOC shows). Falls back to the tabular calendar."""
    try:
        from hijridate import Gregorian
        h = Gregorian(d.year, d.month, d.day).to_hijri()
        return f"{h.day} {h.month_name()} {h.year}"
    except Exception:
        pass
    jd = int(_jd(d) + 0.5)
    l = jd - 1948440 + 10632
    n = (l - 1) // 10631
    l = l - 10631 * n + 354
    j = ((10985 - l) // 5316) * ((50 * l) // 17719) + (l // 5670) * ((43 * l) // 15238)
    l = l - ((30 - j) // 15) * ((17719 * j) // 50) - (j // 16) * ((15238 * j) // 43) + 29
    m = (24 * l) // 709
    day = l - (709 * m) // 24
    year = 30 * n + j - 30
    months = ["Muharram", "Safar", "Rabi al-Awwal", "Rabi al-Thani", "Jumada al-Ula", "Jumada al-Thani",
              "Rajab", "Shaban", "Ramadan", "Shawwal", "Dhu al-Qadah", "Dhu al-Hijjah"]
    return f"{day} {months[m - 1]} {year}"

# ---------- colour ----------
# Sky by sun altitude: (degrees, zenith, middle, horizon). Golden hour, sunset,
# civil-twilight "blue hour" and night each get their own band.
SKY = [
    (-90, (5, 8, 22), (8, 12, 30), (14, 18, 42)),
    (-18, (6, 9, 26), (10, 14, 36), (18, 22, 50)),
    (-12, (10, 14, 40), (20, 26, 62), (46, 42, 80)),
    (-9, (14, 20, 56), (34, 40, 86), (92, 66, 100)),
    (-6, (22, 30, 80), (68, 60, 112), (178, 96, 104)),
    (-3, (34, 46, 106), (118, 86, 132), (236, 122, 92)),
    (0, (44, 62, 126), (150, 110, 142), (250, 144, 88)),
    (3, (54, 86, 152), (150, 142, 172), (250, 172, 112)),
    (8, (60, 106, 182), (128, 156, 202), (245, 202, 150)),
    (15, (50, 116, 202), (98, 160, 222), (196, 214, 228)),
    (30, (40, 112, 206), (80, 152, 226), (166, 206, 236)),
    (90, (34, 104, 200), (70, 144, 222), (150, 198, 236)),
]

def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))

def sky_colours(alt):
    """(zenith, middle, horizon) colours for a sun altitude."""
    for (a0, *c0), (a1, *c1) in zip(SKY, SKY[1:]):
        if a0 <= alt <= a1:
            f = (alt - a0) / (a1 - a0)
            return tuple(lerp(x, y, f) for x, y in zip(c0, c1))
    return tuple(SKY[-1][1:])

def glow_strength(alt):
    """Warm glow on the sun's side: peaks just around sunset, gone by +12 / -10 deg."""
    if alt >= 12 or alt <= -10:
        return 0.0
    return 0.6 * (1 - alt / 12) if alt >= 0 else 0.6 * (1 + alt / 10)

def luminance(c):
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]

# ---------- drawing ----------
def font(size, bold=False):
    idx = 2 if bold else 0  # Avenir Next: 0 Regular, 2 Demi Bold (ttc order varies; fall back)
    for path in ("/System/Library/Fonts/Avenir Next.ttc",):
        try:
            return ImageFont.truetype(path, size * S, index=idx)
        except Exception:
            pass
    return ImageFont.load_default()

def arabic(size):
    return ImageFont.truetype(ARABIC, size * S)

def fmt(h):
    h = h % 24
    hh, mm = int(h), int(round((h - int(h)) * 60))
    if mm == 60:
        hh, mm = hh + 1, 0
    return f"{(hh % 12) or 12}:{mm:02d}"

def ampm(h):
    return "AM" if (h % 24) < 12 else "PM"

def render(now=None):
    now = now or datetime.now(TZ)
    today = now.date()
    pt = prayer_times(today)
    hour = now.hour + now.minute / 60 + now.second / 3600
    alt = sun_altitude(now)
    top, mid, hor = sky_colours(alt)
    ink = (255, 255, 255) if luminance(top) < 150 else (14, 24, 48)
    soft = lerp(ink, top, 0.3)  # secondary text: ink faded 30% toward the sky
    # The prayer row sits low, over the horizon colour, which is much lighter by day.
    row_bg = lerp(mid, hor, 0.8)
    row_ink = (255, 255, 255) if luminance(row_bg) < 165 else (14, 24, 48)
    row_soft = lerp(row_ink, row_bg, 0.3)

    img = Image.new("RGB", (W * S, H * S))
    d = ImageDraw.Draw(img)
    for y in range(H * S):
        f = y / (H * S)
        col = lerp(top, mid, f / 0.5) if f < 0.5 else lerp(mid, hor, ((f - 0.5) / 0.5) ** 0.8)
        d.line([(0, y), (W * S, y)], fill=col)

    # Sun-side glow: the horizon is brightest where the sun is (left in the
    # morning, right in the evening), as in a real sunrise or sunset.
    g = glow_strength(alt)
    if g > 0:
        gx = 40 * S + (W * S - 80 * S) * (hour / 24)
        gy = 345 * S
        glow = Image.new("L", img.size, 0)
        ImageDraw.Draw(glow).ellipse([gx - 330 * S, gy - 170 * S, gx + 330 * S, gy + 170 * S], fill=int(255 * g))
        glow = glow.filter(ImageFilter.GaussianBlur(90 * S))
        warm = lerp((255, 214, 150), (255, 120, 80), min(1, max(0, (6 - alt) / 10)))
        img.paste(warm, mask=glow)
        d = ImageDraw.Draw(img)

    # stars, faded by darkness
    if alt < -6:
        rnd = random.Random(today.toordinal())
        k = min(1, (-6 - alt) / 10)
        for _ in range(140):
            x, y = rnd.uniform(0, W * S), rnd.uniform(0, 300 * S)
            b = int(rnd.uniform(90, 230) * k)
            r = rnd.choice([1, 1, 1, 2]) * S / 2
            d.ellipse([x - r, y - r, x + r, y + r], fill=lerp(top, (255, 255, 240), b / 255))

    # ---- next prayer ----
    order = ["Fajr", "Dhuhr", "Asr", "Maghrib", "Isha"]
    nxt, nxt_t = None, None
    for p in order:
        if pt[p] > hour:
            nxt, nxt_t = p, pt[p]
            break
    if nxt is None:
        nxt, nxt_t = "Fajr", prayer_times(today + timedelta(days=1))["Fajr"] + 24
    mins = int(round((nxt_t - hour) * 60))
    left = f"{mins // 60} h {mins % 60:02d} m" if mins >= 60 else f"{mins} min"

    # Current prayer window. Sunrise to Dhuhr has no fard prayer: show Sunrise,
    # then Duha (voluntary) from 15 min after sunrise, as ISOC does.
    duha = pt["Sunrise"] + DUHA_AFTER_SUNRISE_MIN / 60
    if hour < pt["Fajr"]:
        cur, cur_t = "Isha", prayer_times(today - timedelta(days=1))["Isha"]
    elif hour < pt["Sunrise"]:
        cur, cur_t = "Fajr", pt["Fajr"]
    elif hour < duha:
        cur, cur_t = "Sunrise", pt["Sunrise"]
    elif hour < pt["Dhuhr"]:
        cur, cur_t = "Duha", duha
    else:
        cur, cur_t = [(p, pt[p]) for p in order if pt[p] <= hour][-1]
    if cur == "Sunrise":
        cur_sub = f"Duha from {fmt(duha)} {ampm(duha)}"
    elif cur == "Duha":
        cur_sub = f"since {fmt(cur_t)} {ampm(cur_t)}  ·  optional"
    else:
        cur_sub = f"since {fmt(cur_t)} {ampm(cur_t)}"

    M = 40 * S
    # header
    d.text((M, 30 * S), now.strftime("%A, %B ") + str(now.day), font=font(22, True), fill=ink)
    # The Islamic day starts at Maghrib, so the Hijri date rolls over then.
    hijri_day = today + timedelta(days=1) if hour >= pt["Maghrib"] else today
    d.text((M, 60 * S), hijri(hijri_day), font=font(18), fill=soft)
    clock = now.strftime("%-I:%M")
    cf = font(54, True)
    cw = d.textlength(clock, font=cf)
    d.text((W * S - M - cw - 44 * S, 18 * S), clock, font=cf, fill=ink)
    d.text((W * S - M - 38 * S, 52 * S), now.strftime("%p"), font=font(18, True), fill=soft)

    # ---- hero ----
    # The current prayer is the main thing, with time left in its window and a
    # bar that drains. Amber in the last ENDING_MIN, red in the last URGENT_MIN.
    # Between sunrise and Dhuhr there is no fard prayer, so Dhuhr leads instead.
    gold = (255, 196, 92) if luminance(top) < 150 else (150, 96, 10)
    amber = (255, 176, 64) if luminance(top) < 150 else (176, 96, 0)
    red = (255, 96, 80) if luminance(top) < 150 else (190, 40, 30)
    names = ARABIC_NAMES
    ends = {"Fajr": pt["Sunrise"], "Dhuhr": pt["Asr"], "Asr": pt["Maghrib"], "Maghrib": pt["Isha"],
            "Isha": pt["Fajr"] if hour < pt["Fajr"] else prayer_times(today + timedelta(days=1))["Fajr"] + 24}
    cur_start = cur_t - 24 if cur == "Isha" and hour < pt["Fajr"] else cur_t

    def dur(h):
        m = max(0, int(math.ceil(h * 60 - 1e-9)))
        return f"{m // 60} h {m % 60:02d} m" if m >= 60 else f"{m} min"

    def name_line(x, y, name, size, col, max_w):
        nf = font(size, True)
        d.text((x - 2 * S, y), name, font=nf, fill=col)
        nw = d.textlength(name, font=nf)
        ar, af = names.get(name), arabic(int(size * 0.62))
        if ar and nw + 16 * S + d.textlength(ar, font=af, direction="rtl", language="ar") < max_w:
            d.text((x + nw + 16 * S, y + size * 0.27 * S), ar, font=af, fill=soft, direction="rtl", language="ar")

    hero_w, side_x = 470 * S, M + 500 * S
    side_w = W * S - M - side_x
    if cur in ends:
        end_t = ends[cur]
        remain = end_t - hour
        frac = min(1, max(0, (hour - cur_start) / (end_t - cur_start)))
        if remain * 60 <= URGENT_MIN:
            state_col, label = red, f"NOW · {cur.upper()} ENDS SOON"
        elif remain * 60 <= ENDING_MIN:
            state_col, label = amber, f"NOW · {cur.upper()} ENDING"
        else:
            state_col, label = None, "NOW"
        d.text((M, 106 * S), label, font=font(16, True), fill=ink if state_col else soft)
        name_line(M, 120 * S, cur, 72, ink, hero_w)
        sf = font(24, True)
        left_txt, until_txt = f"{dur(remain)} left", f"until {fmt(end_t)} {ampm(end_t)}"
        if state_col:
            # solid badge so the warning reads on any sky (sunrise/sunset are orange)
            badge = (255, 176, 64) if state_col == amber else (226, 64, 52)
            tw = d.textlength(left_txt, font=sf)
            d.rounded_rectangle([M - 8 * S, 200 * S, M + tw + 8 * S, 234 * S], radius=10 * S, fill=badge)
            d.text((M, 203 * S), left_txt, font=sf, fill=(20, 16, 12) if state_col == amber else (255, 255, 255))
            d.text((M + tw + 22 * S, 203 * S), until_txt, font=sf, fill=ink)
        else:
            d.text((M, 204 * S), f"{left_txt}  ·  {until_txt}", font=sf, fill=ink)
        # draining bar: the part still left is the bright part
        bx, by, bw, bh = M, 242 * S, hero_w - 30 * S, 7 * S
        d.rounded_rectangle([bx, by, bx + bw, by + bh], radius=bh // 2, fill=lerp(top, ink, 0.18))
        left_px = bw * (1 - frac)
        if left_px > bh:
            bar = None if not state_col else ((255, 176, 64) if state_col == amber else (226, 64, 52))
            d.rounded_rectangle([bx + bw - left_px, by, bx + bw, by + bh], radius=bh // 2,
                                fill=bar or lerp(top, ink, 0.75))
        # side: next prayer, small
        d.text((side_x, 106 * S), "NEXT", font=font(16, True), fill=gold)
        name_line(side_x, 124 * S, nxt, 40, ink, side_w)
        d.text((side_x, 178 * S), f"{fmt(nxt_t)} {ampm(nxt_t)}  ·  in {left}", font=font(19, True), fill=soft)
    else:
        # Sunrise / Duha: Dhuhr leads
        d.text((M, 106 * S), "NEXT", font=font(16, True), fill=gold)
        name_line(M, 120 * S, nxt, 72, ink, hero_w)
        d.text((M, 204 * S), f"in {left}  ·  {fmt(nxt_t)} {ampm(nxt_t)}", font=font(24, True), fill=ink)
        d.text((side_x, 106 * S), "NOW", font=font(16, True), fill=soft)
        name_line(side_x, 124 * S, cur, 40, lerp(ink, top, 0.15), side_w)
        d.text((side_x, 178 * S), cur_sub, font=font(19, True), fill=soft)

    # moon (top right, under the clock) when the sun is down
    if alt < -2:
        lit, waxing = moon_phase(now)
        cx, cy, r = W * S - M - cw - 44 * S - 34 * S, 50 * S, 16 * S  # left of the clock
        k = 1 - 2 * lit  # terminator position: 1 new, 0 half, -1 full
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=lerp(top, (244, 240, 220), 0.12))
        for yy in range(-r, r + 1):
            w = math.sqrt(max(0, r * r - yy * yy))
            x0, x1 = (k * w, w) if waxing else (-w, -k * w)  # waxing: lit on the right (northern sky)
            if x1 > x0:
                d.line([(cx + x0, cy + yy), (cx + x1, cy + yy)], fill=(244, 240, 220))

    # ---- 24 h sky band ----
    bx0, bx1 = M, W * S - M
    by_h, amp = 345 * S, 112 * S         # horizon y, px per 90 deg above it
    def X(h):
        return bx0 + (bx1 - bx0) * (h / 24)
    def Y(a):
        return by_h - a / 90 * amp * (1 if a > 0 else 0.35)  # squash night so it clears the row
    pts = []
    for i in range(0, 24 * 12 + 1):
        h = i / 12
        t = datetime(today.year, today.month, today.day, tzinfo=TZ) + timedelta(hours=h)
        pts.append((X(h), Y(sun_altitude(t))))
    # daylight fill under the curve, above the horizon
    glow = Image.new("L", img.size, 0)
    gd = ImageDraw.Draw(glow)
    gd.polygon([(x, min(y, by_h)) for x, y in pts] + [(bx1, by_h), (bx0, by_h)], fill=70)
    img.paste((255, 240, 200), mask=glow)
    d = ImageDraw.Draw(img)
    d.line([(bx0, by_h), (bx1, by_h)], fill=lerp(hor, ink, 0.45), width=S)
    d.line(pts, fill=lerp(hor, ink, 0.75), width=2 * S, joint="curve")

    # prayer ticks on the curve
    for name, _ in PRAYERS:
        h = pt[name]
        t = datetime(today.year, today.month, today.day, tzinfo=TZ) + timedelta(hours=h)
        x, y = X(h), Y(sun_altitude(t))
        r = 4 * S
        is_next = name == nxt and nxt_t < 24
        col = (255, 196, 92) if is_next else ink
        if name == "Sunrise":
            d.ellipse([x - r, y - r, x + r, y + r], outline=col, width=S)
        else:
            d.ellipse([x - r, y - r, x + r, y + r], fill=col)

    # now marker: glowing sun, or a pale dot under the horizon
    nx, ny = X(hour), Y(alt)
    if alt > -0.833:
        halo = Image.new("L", img.size, 0)
        ImageDraw.Draw(halo).ellipse([nx - 26 * S, ny - 26 * S, nx + 26 * S, ny + 26 * S], fill=140)
        halo = halo.filter(ImageFilter.GaussianBlur(10 * S))
        img.paste((255, 226, 150), mask=halo)
        d = ImageDraw.Draw(img)
        d.ellipse([nx - 9 * S, ny - 9 * S, nx + 9 * S, ny + 9 * S], fill=(255, 214, 110))
    else:
        d.ellipse([nx - 6 * S, ny - 6 * S, nx + 6 * S, ny + 6 * S], outline=ink, width=2 * S)
    d.line([(nx, by_h + 6 * S), (nx, by_h + 14 * S)], fill=ink, width=2 * S)

    # ---- five prayers row ----
    row_y = 398 * S
    cell = (bx1 - bx0) / 5
    for i, p in enumerate(order):
        cx = bx0 + cell * i + cell / 2
        active = p == nxt
        if active:
            d.rounded_rectangle([cx - cell / 2 + 6 * S, row_y - 6 * S, cx + cell / 2 - 6 * S, row_y + 64 * S],
                                radius=14 * S, fill=lerp(top, (255, 196, 92), 0.85))
        if p == cur and not active:
            d.rounded_rectangle([cx - cell / 2 + 6 * S, row_y - 6 * S, cx + cell / 2 - 6 * S, row_y + 64 * S],
                                radius=14 * S, outline=row_soft, width=2 * S)
        c = (20, 22, 30) if active else row_ink
        c2 = (60, 50, 30) if active else row_soft
        tf, nf = font(26, True), font(15, True)
        tt = fmt(pt[p])
        d.text((cx - d.textlength(p.upper(), font=nf) / 2, row_y + 2 * S), p.upper(), font=nf, fill=c2)
        d.text((cx - d.textlength(tt, font=tf) / 2, row_y + 22 * S), tt, font=tf, fill=c)

    return img.resize((W, H), Image.LANCZOS)

def png_bytes(img):
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)  # 8-bit RGB, non-interlaced, as the panel wants
    return buf.getvalue()

PAGE = b"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Prayer Sky</title>
<style>
  html, body { margin: 0; height: 100%; background: #0b1026; }
  body { display: flex; align-items: center; justify-content: center; }
  img { width: 100%; max-width: 1200px; height: auto; display: block; }
</style></head>
<body><img id="sky" src="/sky.png" alt="Prayer times over the sky right now">
<script>
  // Same 30 s cadence as the wall panel.
  setInterval(() => { document.getElementById("sky").src = "/sky.png?t=" + Date.now(); }, 30000);
</script></body></html>
"""

class Handler(BaseHTTPRequestHandler):
    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/sky.png":
            self._send(200, "image/png", png_bytes(render()))
        elif path == "/":
            self._send(200, "text/html; charset=utf-8", PAGE)
        elif path == "/healthz":
            self._send(200, "text/plain", b"ok")
        else:
            self.send_error(404)

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"), fmt % args))

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "render"
    if cmd == "serve":
        port = int(sys.argv[2]) if len(sys.argv) > 2 else 8000
        host = sys.argv[3] if len(sys.argv) > 3 else "0.0.0.0"
        print(f"serving http://{host}:{port}/", flush=True)
        ThreadingHTTPServer((host, port), Handler).serve_forever()
    else:
        out = sys.argv[2] if len(sys.argv) > 2 else "sky.png"
        at = None
        if "--at" in sys.argv:
            at = datetime.fromisoformat(sys.argv[sys.argv.index("--at") + 1]).replace(tzinfo=TZ)
        open(out, "wb").write(png_bytes(render(at)))
        print(out)
