(function () {
    var loader = document.getElementById('page-loader');
    if (!loader) {
        return;
    }

    loader.hidden = false;
    var startedAt = Date.now();
    var timeoutId;
    var dismissStarted = false;

    function dismissLoader() {
        if (dismissStarted) {
            return;
        }
        dismissStarted = true;
        window.clearTimeout(timeoutId);
        var minimumVisibleTime = 350;
        var remaining = Math.max(
            0, minimumVisibleTime - (Date.now() - startedAt));

        window.setTimeout(function () {
            loader.classList.add('is-hidden');
            loader.setAttribute('aria-busy', 'false');
            document.documentElement.removeAttribute('aria-busy');
            window.setTimeout(function () {
                loader.hidden = true;
            }, 250);
        }, remaining);
    }

    document.documentElement.setAttribute('aria-busy', 'true');
    timeoutId = window.setTimeout(dismissLoader, 8000);
    window.addEventListener('load', dismissLoader, { once: true });
})();
