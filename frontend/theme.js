/* Apply theme from localStorage (papermint-dark-mode: "1" = dark). Run early to avoid flash. */
(function () {
  var isDark = localStorage.getItem('papermint-dark-mode') === '1';
  document.documentElement.setAttribute('data-theme', isDark ? 'dark' : 'light');
})();
function applyTheme(isDark) {
  document.documentElement.setAttribute('data-theme', isDark ? 'dark' : 'light');
  localStorage.setItem('papermint-dark-mode', isDark ? '1' : '0');
}
