/** Same-origin component for the host's existing conversation. No model-generated control text. */
class JiuwenSecurityApprovals extends HTMLElement {
  connectedCallback() {
    this.root = this.shadowRoot ?? this.attachShadow({ mode: "open" });
    this.generation = 0; this.busy = false; this.closed = false;
    this.timer = setInterval(() => this.refresh(), 1200);
    this.refresh();
  }
  disconnectedCallback() { this.closed = true; this.generation++; clearInterval(this.timer); }
  endpoint(path) {
    const base = new URL(this.getAttribute("endpoint") ?? "/security", location.href);
    if (base.origin !== location.origin) throw new Error("Same origin required");
    base.pathname = base.pathname.replace(/\/$/u, "") + path;
    base.search = ""; base.hash = "";
    return base.href;
  }
  async refresh() {
    if (this.closed || this.busy) return;
    const generation = ++this.generation;
    try {
      const response = await fetch(this.endpoint("/requests"), { credentials: "same-origin", signal: AbortSignal.timeout(5000) });
      if (!response.ok) throw new Error();
      const data = await response.json();
      if (this.closed || generation !== this.generation) return;
      this.root.replaceChildren();
      const style = document.createElement("style");
      style.textContent = ":host{display:block;font:inherit}article{border:1px solid #bdc6d0;border-radius:8px;padding:12px;margin:8px 0}button{font:inherit;padding:7px 12px;margin:5px 8px 0 0}pre{white-space:pre-wrap;overflow-wrap:anywhere}p{margin:7px 0}";
      this.root.append(style);
      for (const request of data.requests) this.render(request);
      this.dispatchEvent(new CustomEvent("jiuwen-security-ready", { bubbles: true }));
    } catch {
      if (!this.closed && generation === this.generation) this.root.textContent = "安全确认暂不可用。";
    }
  }
  render(request) {
    const card = document.createElement("article"); card.setAttribute("aria-label", "安全确认");
    const title = document.createElement("strong"); title.textContent = request.message;
    const detail = document.createElement("p");
    detail.textContent = `${request.level} · ${request.action} → ${request.target} · 仅本次有效`;
    card.append(title, detail);
    if (request.llm) {
      const model = document.createElement("p"); model.textContent = `${request.llm.provider} / ${request.llm.model} · ${request.llm.endpoint}`;
      card.append(model);
    }
    if (request.preview !== null) {
      const preview = document.createElement("pre"); preview.textContent = JSON.stringify(request.preview, null, 2); card.append(preview);
    }
    for (const option of request.options) {
      const button = document.createElement("button"); button.type = "button"; button.textContent = option.label;
      button.onclick = async () => {
        if (this.busy || this.closed) return;
        this.busy = true; this.root.querySelectorAll("button").forEach(b => { b.disabled = true; });
        try {
          const response = await fetch(this.endpoint("/decide"), { method: "POST", credentials: "same-origin", signal: AbortSignal.timeout(5000),
            headers: { "content-type": "application/json" }, body: JSON.stringify({
              approval_id: request.approval_id, version: request.version, choice: option.value,
            }) });
          if (!response.ok) throw new Error();
        } catch { card.textContent = "决定未接受，请检查会话后重试。"; }
        finally { this.busy = false; this.refresh(); }
      };
      card.append(button);
    }
    this.root.append(card);
  }
}
customElements.define("jiuwen-security-approvals", JiuwenSecurityApprovals);
