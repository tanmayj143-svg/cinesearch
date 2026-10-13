"""htmx fragment endpoint: renders shot cards for the search UI."""
import html
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

def esc(v):
    return html.escape(str(v if v is not None else ""))

@router.get("/api/search_ui", response_class=HTMLResponse)
def search_ui(q: str = Query(""), camera_movement: str = "", shot_size: str = "",
              mood: str = "", narrative_label: str = "", limit: int = 40):
    from main import search
    data = search(q=q, camera_movement=camera_movement, shot_size=shot_size,
                  mood=mood, narrative_label=narrative_label, limit=limit)
    cards = [CARD.format(
        shot_id=r["shot_id"], code=esc(r["code"]), thumb=esc(r["thumb"] or ""),
        description=esc((r["description"] or "")[:120]), size=esc(r["shot_size"] or ""),
        mv=esc(r["movement"] or ""), mood=esc(r["mood"] or ""),
        scene=f"· {esc(r['scene_label'])}" if r.get("scene_label") else "",
        transcript=esc((r["transcript"] or "")[:100]),
    ) for r in data["results"]]
    n = len(cards)
    head = f'<p style="color:#b08dc8;font-size:11px;letter-spacing:.05em">{n} shot(s)</p>'
    return HTMLResponse(head + "".join(cards) if cards
                        else '<p style="color:#b08dc8">No matches — try fewer filters.</p>')
