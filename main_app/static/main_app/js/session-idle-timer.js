(function () {
    var timer = document.getElementById('sims-idle-timer');
    if (!timer) {
        return;
    }

    var timeoutSeconds = Number(timer.dataset.timeoutSeconds);
    if (!isFinite(timeoutSeconds) || timeoutSeconds <= 0) {
        console.error('SIMS idle timeout must be a positive number.');
        return;
    }

    var timeoutMilliseconds = timeoutSeconds * 1000;
    var keepaliveIntervalMilliseconds = 5 * 60 * 1000;
    var userKey = timer.dataset.userId || 'unknown';
    var activityKey = 'sims-idle-activity:' + userKey;
    var keepaliveKey = 'sims-idle-keepalive:' + userKey;
    var serverLastActivity = Number(timer.dataset.lastActivity) * 1000;
    var now = Date.now();
    if (!isFinite(serverLastActivity) || serverLastActivity <= 0) {
        serverLastActivity = now;
    }

    var storageAvailable = true;
    function readStoredTimestamp(key) {
        if (!storageAvailable) {
            return 0;
        }
        try {
            var value = Number(window.localStorage.getItem(key));
            return isFinite(value) && value > 0 ? value : 0;
        } catch (error) {
            storageAvailable = false;
            console.error('Unable to read the shared SIMS session timer.', error);
            return 0;
        }
    }

    function writeStoredTimestamp(key, value) {
        if (!storageAvailable) {
            return;
        }
        try {
            window.localStorage.setItem(key, String(value));
        } catch (error) {
            storageAvailable = false;
            console.error('Unable to update the shared SIMS session timer.', error);
        }
    }

    var storedActivity = readStoredTimestamp(activityKey);
    if (
        storedActivity <= now
        && now - storedActivity < timeoutMilliseconds
    ) {
        serverLastActivity = Math.max(serverLastActivity, storedActivity);
    }

    var lastActivityAt = serverLastActivity;
    var lastPersistedActivityAt = serverLastActivity;
    var lastKeepaliveAt = Math.max(
        serverLastActivity,
        readStoredTimestamp(keepaliveKey)
    );
    var redirecting = false;
    var lastRenderedSeconds = null;

    function expireSession() {
        if (redirecting) {
            return;
        }
        redirecting = true;
        var logoutUrl = new URL(timer.dataset.logoutUrl, window.location.href);
        logoutUrl.searchParams.set('reason', 'idle');
        window.location.assign(logoutUrl.toString());
    }

    function renderRemainingTime() {
        var remainingSeconds = Math.max(
            0,
            Math.ceil(
                (lastActivityAt + timeoutMilliseconds - Date.now()) / 1000
            )
        );
        var hours = Math.floor(remainingSeconds / 3600);
        var minutes = Math.floor((remainingSeconds % 3600) / 60);
        var seconds = remainingSeconds % 60;
        if (remainingSeconds !== lastRenderedSeconds) {
            timer.textContent = [
                String(hours).padStart(2, '0'),
                String(minutes).padStart(2, '0'),
                String(seconds).padStart(2, '0')
            ].join(':');
            lastRenderedSeconds = remainingSeconds;
        }

        if (remainingSeconds === 0) {
            expireSession();
        }
    }

    function sendKeepaliveIfDue(timestamp) {
        lastKeepaliveAt = Math.max(
            lastKeepaliveAt,
            readStoredTimestamp(keepaliveKey)
        );
        if (timestamp - lastKeepaliveAt < keepaliveIntervalMilliseconds) {
            return;
        }

        lastKeepaliveAt = timestamp;
        writeStoredTimestamp(keepaliveKey, timestamp);
        fetch(timer.dataset.keepaliveUrl, {
            method: 'GET',
            credentials: 'same-origin',
            headers: {'Accept': 'application/json'}
        }).then(function (response) {
            if (response.redirected) {
                window.location.assign(response.url);
                return;
            }
            if (!response.ok) {
                console.error(
                    'SIMS session keep-alive failed with status '
                    + response.status + '.'
                );
            }
        }).catch(function (error) {
            console.error('SIMS session keep-alive request failed.', error);
        });
    }

    function noteActivity() {
        var timestamp = Date.now();
        if (timestamp >= lastActivityAt + timeoutMilliseconds) {
            expireSession();
            return;
        }

        lastActivityAt = timestamp;
        if (timestamp - lastPersistedActivityAt >= 1000) {
            lastPersistedActivityAt = timestamp;
            writeStoredTimestamp(activityKey, timestamp);
        }
        renderRemainingTime();
        sendKeepaliveIfDue(timestamp);
    }

    window.addEventListener('storage', function (event) {
        if (event.key === activityKey) {
            var sharedActivity = Number(event.newValue);
            if (
                isFinite(sharedActivity)
                && sharedActivity > lastActivityAt
                && sharedActivity <= Date.now()
            ) {
                lastActivityAt = sharedActivity;
                renderRemainingTime();
            }
        } else if (event.key === keepaliveKey) {
            var sharedKeepalive = Number(event.newValue);
            if (isFinite(sharedKeepalive)) {
                lastKeepaliveAt = Math.max(lastKeepaliveAt, sharedKeepalive);
            }
        }
    });

    ['mousemove', 'mousedown', 'keydown', 'touchstart', 'input'].forEach(
        function (eventName) {
            document.addEventListener(
                eventName,
                noteActivity,
                {passive: true}
            );
        }
    );
    document.addEventListener(
        'scroll',
        noteActivity,
        {passive: true, capture: true}
    );
    window.addEventListener('focus', noteActivity);
    document.addEventListener('visibilitychange', function () {
        if (!document.hidden) {
            noteActivity();
        }
    });

    renderRemainingTime();
    window.setInterval(renderRemainingTime, 1000);
}());
