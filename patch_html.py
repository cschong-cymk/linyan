import re

def patch_file(path):
    with open(path, 'r') as f:
        html = f.read()

    # Find the fDiv.innerHTML block
    old_block = """<div class="frame-number">${frame.scene_number || (idx + 1)}</div>
            <div style="font-size: 11px; padding: 10px; color: var(--mol-muted); text-align:center;">
              <strong>${frame.action}</strong>: ${frame.visual}
            </div>
            <div class="frame-action">Preview</div>"""

    new_block = """<div class="frame-number">${frame.scene_number || (idx + 1)}</div>
            <div style="font-size: 11px; padding: 10px; color: var(--mol-muted); text-align:center;">
              <strong>${frame.action}</strong>: ${frame.visual}
            </div>
            <div class="frame-voice" style="padding: 0 10px 10px; text-align: center;">
              <textarea placeholder="Narration / Dialogue..." style="width: 100%; font-size: 11px; resize: vertical; padding: 4px; border-radius: 4px; border: 1px solid rgba(255,255,255,0.1); background: transparent; color: inherit;" rows="2">${frame.narration || ""}</textarea>
            </div>
            <div class="frame-action">Preview</div>"""

    if old_block in html:
        html = html.replace(old_block, new_block)
        print(f"Patched {path}")
    else:
        print(f"Block not found in {path}")
    
    with open(path, 'w') as f:
        f.write(html)

patch_file('/opt/linyan/templates/index.html')
patch_file('/opt/linyan/templates/inner.html')
