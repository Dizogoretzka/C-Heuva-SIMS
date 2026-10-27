(function () {
    var password = document.getElementById('id_password');
    var confirmation = document.getElementById('id_confirm_password');
    var lengthFeedback = document.getElementById('password-length-feedback');
    var matchFeedback = document.getElementById('password-match-feedback');

    if (!password || !confirmation || !lengthFeedback || !matchFeedback) {
        return;
    }

    function updatePasswordFeedback() {
        if (!password.value) {
            lengthFeedback.textContent = '';
            lengthFeedback.classList.remove('text-danger', 'text-success');
        } else if (password.value.length < 8) {
            lengthFeedback.textContent = 'Use at least 8 characters.';
            lengthFeedback.classList.add('text-danger');
            lengthFeedback.classList.remove('text-success');
        } else {
            lengthFeedback.textContent = 'Password meets the minimum length.';
            lengthFeedback.classList.add('text-success');
            lengthFeedback.classList.remove('text-danger');
        }

        if (!confirmation.value) {
            matchFeedback.textContent = '';
            matchFeedback.classList.remove('text-danger', 'text-success');
            confirmation.setAttribute('aria-invalid', 'false');
        } else if (password.value !== confirmation.value) {
            matchFeedback.textContent = 'Passwords do not match.';
            matchFeedback.classList.add('text-danger');
            matchFeedback.classList.remove('text-success');
            confirmation.setAttribute('aria-invalid', 'true');
        } else {
            matchFeedback.textContent = 'Passwords match.';
            matchFeedback.classList.add('text-success');
            matchFeedback.classList.remove('text-danger');
            confirmation.setAttribute('aria-invalid', 'false');
        }
    }

    password.addEventListener('input', updatePasswordFeedback);
    confirmation.addEventListener('input', updatePasswordFeedback);
    updatePasswordFeedback();
})();
