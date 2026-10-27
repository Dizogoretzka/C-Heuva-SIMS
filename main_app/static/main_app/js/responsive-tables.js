(function () {
    Array.from(document.querySelectorAll(".content table")).forEach(function (table, index) {
        var headerRow = table.querySelector("thead tr") ||
            Array.from(table.rows).find(function (row) {
                return row.querySelector("th");
            });

        if (!headerRow) {
            return;
        }

        var headerCells = Array.from(headerRow.cells);
        var headerLabels = headerCells.map(function (cell) {
            return cell.textContent.trim();
        });
        if (!table.id) {
            table.id = "responsive-table-" + index;
        }
        var headerIds = headerCells.map(function (cell, cellIndex) {
            if (!cell.id) {
                cell.id = table.id + "-header-" + cellIndex;
            }
            cell.setAttribute("scope", "col");
            return cell.id;
        });

        table.classList.add("responsive-table");
        if (!headerRow.closest("thead")) {
            headerRow.setAttribute("data-responsive-table-header", "");
        }

        Array.from(table.rows).forEach(function (row) {
            if (row === headerRow) {
                return;
            }

            Array.from(row.cells).forEach(function (cell, index) {
                if (cell.tagName === "TD") {
                    cell.setAttribute(
                        "headers",
                        headerIds.slice(index, index + cell.colSpan).join(" ")
                    );
                    cell.setAttribute(
                        "data-label",
                        cell.colSpan > 1 ? "" : headerLabels[index] || ""
                    );
                }
            });
        });
    });
})();
