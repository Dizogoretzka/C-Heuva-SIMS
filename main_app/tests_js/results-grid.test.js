const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const script = fs.readFileSync(
    path.join(__dirname, '..', 'static', 'main_app', 'js', 'results-grid.js'),
    'utf8'
);

test('learner search filters by name or account ID without removing rows', () => {
    const learners = [
        {
            dataset: { learnerName: 'Alex Learner', accountId: 'learner-a' },
            markInput: { value: '38' }
        },
        {
            dataset: { learnerName: 'Morgan Student', accountId: 'learner-b' },
            markInput: { value: '72' }
        }
    ];
    learners.forEach((row) => {
        row.hidden = false;
        row.querySelectorAll = (selector) => (
            selector === '[data-mark-input]' ? [row.markInput] : []
        );
        row.querySelector = (selector) => {
            if (selector === '[data-final-mark]') {
                return { textContent: '' };
            }
            if (selector === '[data-grade-symbol]') {
                return { textContent: '' };
            }
            if (selector === '[data-grade-description]') {
                return { textContent: '' };
            }
            return null;
        };
    });

    const noMatches = { hidden: true };
    const formListeners = {};
    const searchListeners = {};
    const weightTotal = { textContent: '' };
    const weightWarning = { hidden: false };
    const form = {
        addEventListener: (name, callback) => {
            formListeners[name] = callback;
        },
        querySelector: (selector) => {
            if (selector === '[data-weight-total]') {
                return weightTotal;
            }
            if (selector === '[data-weight-warning]') {
                return weightWarning;
            }
            if (selector === '[data-no-learner-matches]') {
                return noMatches;
            }
            return null;
        },
        querySelectorAll: (selector) => {
            if (selector === '[data-learner-row]') {
                return learners;
            }
            return [];
        }
    };
    const learnerSearch = {
        value: '',
        addEventListener: (name, callback) => {
            searchListeners[name] = callback;
        }
    };
    const document = {
        querySelector: (selector) => {
            if (selector === '[data-results-grid]') {
                return form;
            }
            if (selector === '[data-learner-search]') {
                return learnerSearch;
            }
            return null;
        },
        getElementById: () => ({ textContent: '[]' })
    };

    vm.runInNewContext(script, {
        document,
        window: { addEventListener: () => {} }
    });

    learnerSearch.value = 'alex';
    searchListeners.input();
    assert.deepEqual(learners.map((row) => row.hidden), [false, true]);
    assert.deepEqual(
        learners.map((row) => row.markInput.value),
        ['38', '72']
    );
    assert.equal(noMatches.hidden, true);

    learnerSearch.value = 'LEARNER-B';
    searchListeners.input();
    assert.deepEqual(learners.map((row) => row.hidden), [true, false]);

    learnerSearch.value = 'not-found';
    searchListeners.input();
    assert.deepEqual(learners.map((row) => row.hidden), [true, true]);
    assert.equal(noMatches.hidden, false);

    learnerSearch.value = '';
    searchListeners.input();
    assert.deepEqual(learners.map((row) => row.hidden), [false, false]);
    assert.equal(noMatches.hidden, true);
    assert.equal(formListeners.input !== undefined, true);
});
