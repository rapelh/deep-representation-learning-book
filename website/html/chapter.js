/* chapter.js — Interactive features for make4ht chapter HTML output */

document.addEventListener("DOMContentLoaded", function () {
  "use strict";

  function prependAnchor(host, targetId, ariaLabel, className) {
    if (!host || !targetId) return;
    if (host.querySelector(".heading-anchor, .heading-anchor-outside")) return;

    var anchor = document.createElement("a");
    anchor.href = "#" + targetId;
    anchor.className = className || "heading-anchor";
    anchor.setAttribute("aria-label", ariaLabel);
    anchor.textContent = "\uD83D\uDD17"; // 🔗
    host.insertBefore(anchor, host.firstChild);
  }

  function scrollToHashTarget() {
    if (!window.location.hash) return;
    try {
      var target = document.querySelector(window.location.hash);
      if (target) {
        target.scrollIntoView({ behavior: "smooth", block: "start" });
      }
    } catch (_) {}
  }

  function afterMathJaxLayout(callback) {
    if (window.MathJax && window.MathJax.startup) {
      window.MathJax.startup.promise.then(callback);
    } else {
      setTimeout(callback, 800);
    }
  }

  function trimEdgeText(node, pattern) {
    if (!node || node.nodeType !== 3) return;
    node.textContent = node.textContent.replace(pattern, "");
  }

  // =========================================================================
  // 1. Heading anchor links (sections, subsections, paragraphs)
  // =========================================================================

  document.querySelectorAll("h2[id], h3[id], h4[id]").forEach(function (heading) {
    prependAnchor(
      heading,
      heading.id,
      "Copy link to this section",
      "heading-anchor"
    );
  });

  // =========================================================================
  // 2. Footnote hover popovers
  // =========================================================================

  var currentPop = null;

  function removePop() {
    if (currentPop && currentPop.parentNode) {
      currentPop.parentNode.removeChild(currentPop);
    }
    currentPop = null;
  }

  document
    .querySelectorAll('a.footnote-mark, sup.textsuperscript a[href^="#"]')
    .forEach(function (mark) {
      var href = mark.getAttribute("href");
      if (!href || !href.startsWith("#")) return;

      var fnId = href.slice(1);
      var fnContent = document.getElementById(fnId);
      if (!fnContent) return;

      var trigger = mark.closest("sup") || mark;

      trigger.addEventListener("mouseenter", function () {
        removePop();

        var pop = document.createElement("div");
        pop.className = "footnote-pop";

        var clone = fnContent.cloneNode(true);
        clone.querySelectorAll("a.footnote-backref").forEach(function (a) {
          a.remove();
        });
        var firstSup = clone.querySelector("sup");
        if (firstSup && /^\d+$/.test(firstSup.textContent.trim())) {
          firstSup.remove();
        }

        pop.innerHTML = clone.innerHTML || clone.textContent;
        document.body.appendChild(pop);

        var rect = trigger.getBoundingClientRect();
        var popRect = pop.getBoundingClientRect();
        pop.style.position = "absolute";
        pop.style.top = window.scrollY + rect.bottom + 6 + "px";
        pop.style.left =
          Math.min(
            rect.left,
            window.innerWidth - popRect.width - 12
          ) + "px";

        currentPop = pop;
      });

      trigger.addEventListener("mouseleave", function () {
        setTimeout(removePop, 120);
      });
    });

  // Global click dismisses popover
  document.addEventListener("click", function (e) {
    if (currentPop && !e.target.closest(".footnote-pop")) {
      removePop();
    }
  });

  // =========================================================================
  // 3. Smooth scroll for internal links
  // =========================================================================

  document.querySelectorAll('a[href^="#"]').forEach(function (a) {
    a.addEventListener("click", function (e) {
      var href = a.getAttribute("href");
      if (!href || href === "#") return;

      var target = document.querySelector(href);
      if (!target) return;

      e.preventDefault();
      history.pushState(null, "", href);
      target.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  });

  // =========================================================================
  // 4. Algorithm caption formatting
  // =========================================================================

  document
    .querySelectorAll(".algorithm-container .caption")
    .forEach(function (cap) {
      if (cap.dataset.algCaptionWrapped) return;

      var tag = cap.querySelector(".id");
      if (!tag) return;

      var wrapper = document.createElement("span");
      wrapper.className = "alg-caption-text";
      while (tag.nextSibling) {
        wrapper.appendChild(tag.nextSibling);
      }
      if (!wrapper.firstChild) return;

      trimEdgeText(wrapper.firstChild, /^\s+/);
      trimEdgeText(wrapper.lastChild, /\.\s*$|\s+$/);

      tag.insertAdjacentText("afterend", " (");
      tag.nextSibling.after(wrapper);
      wrapper.insertAdjacentText("afterend", ").");

      cap.dataset.algCaptionWrapped = "1";
    });

  // =========================================================================
  // 5. Re-scroll to hash target after MathJax renders
  // =========================================================================

  if (window.location.hash) {
    afterMathJaxLayout(scrollToHashTarget);
  }
});
