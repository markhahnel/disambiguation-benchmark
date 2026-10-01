/**
 * Iframe height listener for openresearch.wtf figure embeds.
 *
 * MANUAL STEP: paste this whole file into Ghost Admin -> Settings ->
 * Code injection -> Site footer, inside a <script> tag. It lives in
 * Ghost once, for all posts. Until it is in place, figures render at
 * the fallback height set inline on each iframe.
 *
 * Every figure document served from figures.openresearch.wtf posts
 *   { type: "orw-figure-height", id: "<project>/<version>/<figure>", height: N }
 * to its parent on load and on ResizeObserver fire. This listener
 * validates the origin, matches the message to the sending iframe, and
 * sets the iframe height.
 *
 * SITE-WIDE, SHARED WITH SIBLING PROJECTS: one copy of this script serves
 * every post on the site, so it must keep working for figures published by
 * the other projects in this repo. It is a superset of the collision
 * project's copy: same origin check, same message shape, same
 * contentWindow-first matching, plus an id-based fallback for the case
 * where the sending window cannot be compared. Keep it that way.
 */
(function () {
  "use strict";
  var FIGURE_ORIGIN = "https://figures.openresearch.wtf";
  var MAX_HEIGHT_PX = 12000;

  window.addEventListener("message", function (event) {
    if (event.origin !== FIGURE_ORIGIN) return;
    var data = event.data;
    if (!data || data.type !== "orw-figure-height") return;
    if (typeof data.id !== "string" || typeof data.height !== "number") return;
    if (!isFinite(data.height) || data.height <= 0 || data.height > MAX_HEIGHT_PX) return;

    var height = Math.ceil(data.height) + "px";
    var iframes = document.querySelectorAll("figure.orw-figure iframe");
    var byId = null;
    for (var i = 0; i < iframes.length; i++) {
      var frame = iframes[i];
      // Match on the sending window first (unforgeable), then confirm
      // the declared id against the iframe src as a belt-and-braces check.
      if (frame.contentWindow === event.source) {
        if (frame.src.indexOf(data.id) === -1) return;
        frame.style.height = height;
        return;
      }
      // Remember the frame whose src is exactly this id, in case no
      // contentWindow matched (a cross-origin frame the browser will not let
      // us compare, or a message relayed after the frame was replaced).
      if (byId === null && frame.src === FIGURE_ORIGIN + "/" + data.id + ".html") {
        byId = frame;
      }
    }
    // Safe as a fallback because the origin check above already proved the
    // message came from the figure host; the id only picks which frame.
    if (byId !== null) byId.style.height = height;
  });
})();
