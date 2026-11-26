/* ChatPage adapter for the existing authenticated /v1/messages API. */
(() => {
  "use strict";
  const pages = [];
  let lastViewer = "";
  let sequence = 0;
  const images = /https?:\/\/192\.168\.8\.160:15043\/(?:api|aiwebapi)\/ai\/patients\/report-artifacts\/(irego_scope_v2_[0-9a-f]{64})\/content(?:\?[^\s<>\[\]()"'\\]*)?/g;
  const relay = "/chatPage/_report-history/messages";

  function history(source, options) {
    const original = new URL(source, location.href);
    const localAgent = original.origin === "http://192.168.8.160:5080" && original.pathname === "/v1/messages";
    const siteAgent = original.origin === location.origin && original.pathname === "/api2/messages";
    if (localAgent || siteAgent) {
      const target = new URL(relay, location.origin);
      target.search = original.search;
      return fetch(target, {...options, mode: "same-origin", redirect: "error", cache: "no-store"});
    }
    return fetch(source, options);
  }

  function direct(src) {
    return src.replace(/^(https?:\/\/[^/]+:15043)\/aiwebapi\/ai\/patients\/report-artifacts\//,
                       "$1/api/ai/patients/report-artifacts/");
  }

  function prepare(body, source, token) {
    const request = new URL(source, location.href);
    const ownProxy = request.origin === location.origin && ["/api2/messages", relay].includes(request.pathname);
    const knownAgent = ["http://192.168.8.160:5080", "http://114.80.41.254:8084"].includes(request.origin)
                      && request.pathname === "/v1/messages";
    const cid = request.searchParams.get("conversation_id");
    if ((!ownProxy && !knownAgent) || !token || !cid || !Array.isArray(body.data)) return body;
    const viewer = request.searchParams.get("user") + "\n" + token;
    if (lastViewer !== viewer) pages.length = 0;
    lastViewer = viewer;
    const page = new Map();
    for (const row of body.data) {
      if (row.conversation_id !== cid || typeof row.answer !== "string" || typeof row.id !== "string"
          || !row.id.startsWith(cid + ":") || !/^\d+$/.test(row.id.slice(cid.length + 1))) continue;
      const sameTurn = new Map();
      row.answer = row.answer.replace(images, (url, ref) => {
        if (sameTurn.has(ref)) return sameTurn.get(ref);
        const marker = new URL(relay + "#image-" + (++sequence), location.origin).href;
        const lookup = new URL(relay, location.origin);
        lookup.search = request.search;
        lookup.searchParams.set("artifact_ref", ref);
        lookup.searchParams.set("message_id", row.id);
        page.set(marker, (signal) => fetch(lookup, {
          headers: {Authorization: "Bearer " + token}, mode: "same-origin",
          redirect: "error", cache: "no-store", signal
        }));
        sameTurn.set(ref, marker);
        return marker;
      });
    }
    if (page.size) {
      pages.push(page);
      if (pages.length > 8) pages.shift();
    }
    return body;
  }

  function image(React, jsx, props) {
    const src = direct(props.src);
    const owned = pages.find(page => page.has(src));
    const load = owned && owned.get(src);
    const [result, setResult] = React.useState(null);
    const [attempt, setAttempt] = React.useState(0);
    const element = React.useRef(null);
    React.useEffect(() => {
      if (!load) return;
      const abort = new AbortController();
      let objectURL;
      async function show() {
        try {
          const response = await load(abort.signal);
          if (!response.ok || response.headers.get("Content-Type")?.split(";")[0] !== "image/png") {
            setResult({src, failed: true, retry: response.status >= 500 || response.status === 429});
            return;
          }
          const bytes = await response.blob();
          if (abort.signal.aborted) return;
          objectURL = URL.createObjectURL(bytes);
          setResult({src, url: objectURL});
        } catch {
          if (!abort.signal.aborted) setResult({src, failed: true, retry: true});
        }
      }
      let observer;
      if (window.IntersectionObserver && element.current) {
        observer = new IntersectionObserver(entries => {
          if (entries.some(entry => entry.isIntersecting)) { observer.disconnect(); show(); }
        }, {rootMargin: "200px"});
        observer.observe(element.current);
      } else { show(); }
      return () => { abort.abort(); observer?.disconnect(); if (objectURL) URL.revokeObjectURL(objectURL); };
    }, [src, load, attempt]);
    if (result?.src === src && result.failed) {
      if (result.retry) {
        return jsx("button", {type: "button", onClick: () => { setResult(null); setAttempt(a => a + 1); },
          children: "图片暂时无法读取，点击重试"});
      }
      return jsx("div", {style: {color: props.textColor, padding: "0.4rem 0"}, children: load ? "当前无法查看这份报告" : "⚠ 图片已失效"});
    }
    if (load && !(result?.src === src && result.url)) {
      return jsx("div", {ref: element, role: "status", "aria-label": "正在读取报告图片", style: {minHeight: 24}});
    }
    return jsx("img", {src: load ? result.url : src, alt: "", loading: "lazy",
      onError: () => setResult({src, failed: true}), style: {display: "block", maxWidth: "100%", borderRadius: "8px", marginBottom: "0.25rem"}});
  }

  window.chatReportImages = {prepare, image, history};
})();
