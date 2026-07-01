// overview.js — нормализует inline-SVG диаграммы и, если с CDN загрузился svg-pan-zoom,
// включает pan/zoom: кнопки +/−/reset, колесо — зум, тащить — двигать, тач/пинч; клики
// по узлам ($link) сохраняются. Либа не загрузилась — остаётся чистый статический SVG.
(function () {
  document.querySelectorAll(".diagram svg").forEach(function (svg) {
    // PlantUML пишет жёсткие пиксели (style="width:NNNpx" + width/height) и
    // preserveAspectRatio="none". Снимаем фиксацию и чиним соотношение сторон, чтобы SVG
    // корректно масштабировался И статически, И под pan/zoom. viewBox у PlantUML уже есть.
    svg.style.removeProperty("width");
    svg.style.removeProperty("height");
    if (!svg.getAttribute("viewBox")) {
      try {
        var b = svg.getBBox();
        if (b.width && b.height) {
          svg.setAttribute("viewBox", b.x + " " + b.y + " " + b.width + " " + b.height);
        }
      } catch (e) { /* SVG ещё не отрисован — оставляем как есть */ }
    }
    svg.setAttribute("preserveAspectRatio", "xMidYMid meet");
    svg.setAttribute("width", "100%");
    svg.removeAttribute("height"); // статически высота — из соотношения сторон (CSS height:auto)

    if (typeof window.svgPanZoom !== "function") return; // CDN недоступен → статический SVG
    svg.parentElement.classList.add("pannable");
    svg.setAttribute("height", "100%");
    window.svgPanZoom(svg, {
      zoomEnabled: true,
      controlIconsEnabled: true, // экранные кнопки +/−/reset
      fit: true,
      center: true,
      minZoom: 0.2,
      maxZoom: 20,
    });
  });
})();
