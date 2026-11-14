(function () {
    var page = document.querySelector('[data-role-switcher]');
    if (!page) {
        return;
    }

    var roles = ['admin', 'teacher', 'learner'];
    var roleNames = {
        admin: 'Administrator',
        teacher: 'Teacher',
        learner: 'Learner'
    };
    var radios = Array.prototype.slice.call(
        page.querySelectorAll('input[name="selected_role"]'));
    var panels = Array.prototype.slice.call(
        page.querySelectorAll('[data-role-panel]'));
    var heading = page.querySelector('[data-role-heading]');
    var loader = document.getElementById('page-loader');

    function validRole(role) {
        return roles.indexOf(role) !== -1;
    }

    function selectRole(role, updateAddress) {
        if (!validRole(role)) {
            return;
        }

        radios.forEach(function (radio) {
            radio.checked = radio.value === role;
        });
        panels.forEach(function (panel) {
            panel.hidden = panel.getAttribute('data-role-panel') !== role;
        });
        if (heading) {
            heading.textContent = roleNames[role];
        }
        if (updateAddress && window.location.hash !== '#' + role) {
            window.location.hash = role;
        }
    }

    function clearInactiveCredentials(activeRole) {
        roles.forEach(function (role) {
            if (role === activeRole) {
                return;
            }
            var form = page.querySelector(
                '[data-role-form="' + role + '"]');
            if (form) {
                form.querySelectorAll(
                    'input[name="account_id"], input[name="username"], input[name="password"], input[name="pin"]'
                ).forEach(function (input) {
                    input.value = '';
                });
            }
        });
    }

    var hashRole = window.location.hash.slice(1).toLowerCase();
    var initialRole = validRole(hashRole)
        ? hashRole
        : page.getAttribute('data-initial-role');
    selectRole(initialRole, false);

    radios.forEach(function (radio) {
        radio.addEventListener('change', function () {
            if (radio.checked) {
                clearInactiveCredentials(radio.value);
                selectRole(radio.value, true);
            }
        });
    });

    window.addEventListener('hashchange', function () {
        var role = window.location.hash.slice(1).toLowerCase();
        if (validRole(role)) {
            clearInactiveCredentials(role);
            selectRole(role, false);
        }
    });

    page.querySelectorAll('[data-toggle-pin]').forEach(function (button) {
        button.addEventListener('click', function () {
            var pin = document.getElementById(
                button.getAttribute('aria-controls'));
            if (!pin) {
                return;
            }
            var showPin = pin.type === 'password';
            pin.type = showPin ? 'text' : 'password';
            button.setAttribute(
                'aria-label', showPin ? 'Hide PIN' : 'Show PIN');
            var icon = button.querySelector('span');
            if (icon) {
                icon.classList.toggle('fa-eye', !showPin);
                icon.classList.toggle('fa-eye-slash', showPin);
            }
        });
    });

    page.querySelectorAll('form[data-role-form]').forEach(function (form) {
        form.addEventListener('submit', function () {
            if (loader) {
                loader.hidden = false;
                loader.classList.remove('is-hidden');
                loader.setAttribute('aria-busy', 'true');
                document.documentElement.setAttribute('aria-busy', 'true');
            }
        });
    });
})();
