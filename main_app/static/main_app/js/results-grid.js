(function () {
    'use strict';

    var form = document.querySelector('[data-results-grid]');
    if (!form) {
        return;
    }

    var learnerSearch = document.querySelector('[data-learner-search]');
    var weightTotal = form.querySelector('[data-weight-total]');
    var weightWarning = form.querySelector('[data-weight-warning]');
    var unsavedStatus = form.querySelector('[data-unsaved-status]');
    var gradingScale = JSON.parse(
        document.getElementById('results-grading-scale').textContent);
    var isSubmitting = false;

    function assessmentSettings() {
        return Array.prototype.map.call(
            form.querySelectorAll('[data-assessment-row]'),
            function (row) {
                var weightInput = row.querySelector('[data-assessment-weight]');
                var maximumInput = row.querySelector('[data-assessment-maximum]');
                var nameInput = row.querySelector('input[name$="_name"]');
                var weight = Number(weightInput.value);
                var maximum = Number(maximumInput.value);
                return {
                    index: Array.prototype.indexOf.call(
                        row.parentNode.children, row),
                    weight: weight,
                    maximum: maximum,
                    weightText: weightInput.value,
                    maximumText: maximumInput.value,
                    name: nameInput.value,
                    valid: Number.isFinite(weight) && weight >= 0 &&
                        weight <= 100 && Number.isFinite(maximum) &&
                        maximum > 0 && maximum <= 99999.99
                };
            }
        );
    }

    function updateTotals() {
        var assessments = assessmentSettings();
        var total = assessments.reduce(function (sum, assessment) {
            return sum + (Number.isFinite(assessment.weight) ? assessment.weight : 0);
        }, 0);
        var validTotal = assessments.length > 0 &&
            assessments.every(function (assessment) { return assessment.valid; }) &&
            Math.abs(total - 100) < 0.005;

        weightTotal.textContent = total.toFixed(2).replace(/\.?0+$/, '') + '%';
        weightWarning.hidden = validTotal;

        Array.prototype.forEach.call(
            form.querySelectorAll('[data-assessment-heading]'),
            function (heading) {
                var assessment = assessments[Number(heading.dataset.rowIndex)];
                if (assessment) {
                    heading.textContent = '(' + assessment.weightText +
                        '% / ' + assessment.maximumText + ')';
                }
            }
        );
        Array.prototype.forEach.call(
            form.querySelectorAll('[data-assessment-name]'),
            function (heading) {
                var assessment = assessments[Number(heading.dataset.rowIndex)];
                if (assessment) {
                    heading.textContent = assessment.name;
                }
            }
        );

        Array.prototype.forEach.call(
            form.querySelectorAll('[data-learner-row]'),
            function (learnerRow) {
                var markInputs = learnerRow.querySelectorAll('[data-mark-input]');
                var values = [];
                var incomplete = !validTotal || assessments.length === 0;

                Array.prototype.forEach.call(markInputs, function (input, index) {
                    var value = input.value.trim();
                    var maximum = assessments[index] &&
                        assessments[index].maximum;
                    if (value === '') {
                        incomplete = true;
                        values.push(null);
                        return;
                    }
                    var score = Number(value);
                    if (!Number.isFinite(score) || score < 0 ||
                            !Number.isFinite(maximum) || maximum <= 0 ||
                            score > maximum) {
                        incomplete = true;
                        values.push(null);
                        return;
                    }
                    values.push(score);
                });

                var finalCell = learnerRow.querySelector('[data-final-mark]');
                var gradeCell = learnerRow.querySelector('[data-grade-symbol]');
                var descriptionCell = learnerRow.querySelector(
                    '[data-grade-description]');
                if (learnerRow.querySelector('.badge-warning') &&
                        values.every(function (value) { return value === null; })) {
                    return;
                }
                if (incomplete || values.length !== assessments.length) {
                    finalCell.textContent = 'Incomplete';
                    gradeCell.textContent = '-';
                    descriptionCell.textContent = '-';
                    return;
                }

                var finalMark = assessments.reduce(function (sum, assessment, index) {
                    return sum + (values[index] / assessment.maximum) *
                        assessment.weight;
                }, 0);
                finalMark = Math.round((finalMark + Number.EPSILON) * 100) / 100;
                var grade = gradingScale.find(function (item) {
                    return finalMark >= item.minimum;
                });
                finalCell.textContent = finalMark.toFixed(2);
                gradeCell.textContent = grade.symbol;
                descriptionCell.textContent = grade.description;
            }
        );
    }

    function updateLearnerFilter() {
        if (!learnerSearch) {
            return;
        }
        var query = learnerSearch.value.trim().toLocaleLowerCase();
        var rows = form.querySelectorAll('[data-learner-row]');
        var matchCount = 0;
        Array.prototype.forEach.call(rows, function (row) {
            var searchable = (
                (row.dataset.learnerName || '') + ' ' +
                (row.dataset.accountId || '')
            ).toLocaleLowerCase();
            row.hidden = query !== '' && !searchable.includes(query);
            if (!row.hidden) {
                matchCount += 1;
            }
        });
        var noMatches = form.querySelector('[data-no-learner-matches]');
        if (noMatches) {
            noMatches.hidden = query === '' || matchCount > 0;
        }
    }

    function updateUnsavedState() {
        var changed = false;
        Array.prototype.forEach.call(
            form.querySelectorAll('[data-unsaved-input]'),
            function (input) {
                var isChanged = input.value !==
                    (input.getAttribute('data-original-value') || '');
                input.classList.toggle('is-unsaved', isChanged);
                if (isChanged) {
                    changed = true;
                }
            }
        );
        if (unsavedStatus) {
            unsavedStatus.hidden = !changed;
        }
        return changed;
    }

    form.addEventListener('input', function (event) {
        if (event.target.matches(
                '[data-assessment-weight], [data-assessment-maximum], ' +
                '[data-mark-input], input[name$="_name"]')) {
            updateTotals();
        }
        if (event.target.matches('[data-unsaved-input]')) {
            updateUnsavedState();
        }
    });

    form.addEventListener('click', function (event) {
        var removeButton = event.target.closest('[data-remove-assessment]');
        if (removeButton && !window.confirm(
                'Remove this assessment and delete its marks?')) {
            event.preventDefault();
        }
    });

    if (learnerSearch) {
        learnerSearch.addEventListener('input', updateLearnerFilter);
    }
    form.addEventListener('submit', function () {
        isSubmitting = true;
    });
    window.addEventListener('beforeunload', function (event) {
        if (!isSubmitting && updateUnsavedState()) {
            event.preventDefault();
            event.returnValue = '';
        }
    });

    updateLearnerFilter();
    updateUnsavedState();
    updateTotals();
}());
