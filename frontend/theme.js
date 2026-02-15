/* Apply theme from localStorage (dark-mode: "1" = dark). Run early to avoid flash. */
(function () {
  var isDark = localStorage.getItem('resonance-dark-mode') === '1';
  document.documentElement.setAttribute('data-theme', isDark ? 'dark' : 'light');
})();
function applyTheme(isDark) {
  document.documentElement.setAttribute('data-theme', isDark ? 'dark' : 'light');
  localStorage.setItem('resonance-dark-mode', isDark ? '1' : '0');
}
