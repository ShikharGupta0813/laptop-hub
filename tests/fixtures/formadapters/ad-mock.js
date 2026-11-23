// Imitates the custom widgets of the invented form-adapter fixtures (pop-up lists, Yes/No buttons, step buttons). Test-only, no network.
(function () {
  function closeLists() { document.querySelectorAll('ul.mock-list').forEach(function (u) { u.remove(); }); }
  function show(input) {
    closeLists();
    var opts = JSON.parse(input.getAttribute('data-options') || '[]');
    var q = (input.value || '').toLowerCase();
    if (input.hasAttribute('data-search') && q) opts = opts.filter(function (o) { return o.toLowerCase().indexOf(q) >= 0; });
    var ul = document.createElement('ul');
    ul.className = 'mock-list';
    ul.setAttribute('role', 'listbox');
    opts.forEach(function (o) {
      var li = document.createElement('li');
      li.setAttribute('role', 'option');
      li.textContent = o;
      li.style.cssText = 'padding:4px 8px;list-style:none;cursor:pointer';
      li.addEventListener('click', function () {
        var box = input.getAttribute('data-valuebox');
        if (box) { document.getElementById(box).textContent = o; input.value = ''; } else { input.value = o; }
        closeLists();
      });
      ul.appendChild(li);
    });
    ul.style.cssText = 'position:absolute;background:#fff;border:1px solid #888;margin:0;padding:0;z-index:50';
    input.parentNode.appendChild(ul);
  }
  document.querySelectorAll('input[data-options]').forEach(function (i) {
    i.addEventListener('click', function () { show(i); });
    i.addEventListener('input', function () { show(i); });
  });
  document.querySelectorAll('button[data-yesno]').forEach(function (b) {
    b.addEventListener('click', function () {
      b.parentNode.querySelectorAll('button').forEach(function (o) { o.setAttribute('aria-pressed', 'false'); o.classList.remove('_active_1x'); });
      b.setAttribute('aria-pressed', 'true');
      b.classList.add('_active_1x');
    });
  });
  document.querySelectorAll('[data-next]').forEach(function (b) {
    b.addEventListener('click', function (e) { e.preventDefault(); location.href = b.getAttribute('data-next'); });
  });
  document.querySelectorAll('[data-submit]').forEach(function (b) {
    b.addEventListener('click', function (e) { e.preventDefault(); fetch('/submit', { method: 'POST', body: 'x' }); });
  });
})();
