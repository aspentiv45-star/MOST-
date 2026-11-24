// ==UserScript==
// @name         MOST — реклама в веб-плеерах
// @namespace    https://github.com/aspentiv45-star/MOST-
// @version      1.0.0
// @description  Скрывает помеченные рекламные баннеры и нажимает доступную кнопку пропуска на YouTube.
// @match        https://www.youtube.com/*
// @match        https://m.youtube.com/*
// @match        https://open.spotify.com/*
// @match        https://soundcloud.com/*
// @match        https://www.soundcloud.com/*
// @run-at       document-idle
// @grant        none
// @noframes
// ==/UserScript==

(() => {
  "use strict";

  const youtube = ["www.youtube.com", "m.youtube.com"].includes(location.hostname);
  const banners = youtube
    ? ["ytd-display-ad-renderer", "ytd-promoted-sparkles-web-renderer", "ytd-in-feed-ad-layout-renderer"]
    : ['aside[aria-label="Advertisement" i]', 'aside[aria-label="Реклама" i]',
       'aside[aria-label="Werbung" i]', 'iframe[title="Advertisement" i]'];

  if (location.hostname === "open.spotify.com") banners.push('[data-testid="leaderboard-ad"]');
  if (location.hostname.endsWith("soundcloud.com")) banners.push(".advertising__banner");

  const style = document.createElement("style");
  style.textContent = banners.join(",\n") + " { display: none !important; }";
  document.head.append(style);

  if (!youtube) return;
  const skipSelector = "button.ytp-skip-ad-button, button.ytp-ad-skip-button, button.ytp-ad-skip-button-modern";
  function skipWhenAvailable() {
    const player = document.querySelector("#movie_player.ad-showing");
    if (!player) return;
    for (const button of player.querySelectorAll(skipSelector)) {
      if (button.disabled || button.getAttribute("aria-disabled") === "true" ||
          !button.getClientRects().length || getComputedStyle(button).visibility !== "visible") continue;
      button.click();
      break;
    }
  }

  skipWhenAvailable();
  window.setInterval(skipWhenAvailable, 1000);
})();
