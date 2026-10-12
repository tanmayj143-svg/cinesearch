"""htmx fragment endpoint: renders shot cards for the search UI."""
from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse

router = APIRouter()

CARD = """<div class="card" onclick="openShot({shot_id})" data-code="{code}">
  <img src="/{thumb}" loading="lazy" onerror="this.style.display='none'">
  <div class="code">{code}</div>
  <div>{description}</div>
  <div class="tags">{size} · {mv} · {mood} {scene}</div>
  <div class="tags">{transcript}</div>
</div>"""

@router.get("/api/search_ui", response_class=HTMLResponse)
def search_ui(q: str = Query(""), camera_movement: str = "", shot_size: str = ""):
    from main import search
    data = search(q=q, camera_movement=camera_movement, shot_size=shot_size)
    cards = [CARD.format(
        shot_id=r["shot_id"], code=r["code"], thumb=r["thumb"] or "",
        description=(r["description"] or "")[:120], size=r["shot_size"] or "",
        mv=r["movement"] or "", mood=r["mood"] or "",
        scene=f"· {r['scene_label']}" if r.get("scene_label") else "",
        transcript=(r["transcript"] or "")[:100],
    ) for r in data["results"]]
    return HTMLResponse("".join(cards) or '<p style="color:#7d8896">No results.</p>')
