/* Test-only imitation of Workday's widgets, so the real-browser test has something to click. Invented; not Workday code.
   - button[aria-haspopup=listbox]: click opens a ul[role=listbox] in <body>; choosing an option sets the button text.
   - input[data-automation-id=searchBox]: Enter shows an activeListContainer of options; choosing one adds a chip.
   - button[data-template]: Add clones <template> (replacing __N__) into the section's .entries box.
   - "I currently work here" hides the To date.
   - data-next buttons go to the next page; data-submit buttons tell the server (the test fails if that ever happens). */
(function () {
  function closeList() {
    document.querySelectorAll('ul[role="listbox"]').forEach(function (e) { e.remove(); });
    document.querySelectorAll('[data-automation-id="activeListContainer"]').forEach(function (e) { e.remove(); });
  }
  function optionsOf(el) { try { return JSON.parse(el.getAttribute('data-options') || '[]'); } catch (e) { return []; } }
  document.addEventListener('click', function (ev) {
    var t = ev.target.closest('button,[role="option"]');
    if (!t) { return; }
    if (t.matches('button[aria-haspopup="listbox"]')) {
      var was = document.querySelector('ul[role="listbox"]');
      closeList();
      if (was && was.getAttribute('data-for') === t.id) { return; }
      var ul = document.createElement('ul');
      ul.setAttribute('role', 'listbox');
      ul.setAttribute('data-for', t.id);
      optionsOf(t).forEach(function (o) {
        var li = document.createElement('li');
        li.setAttribute('role', 'option');
        li.textContent = o;
        li.addEventListener('click', function () { t.textContent = o; t.setAttribute('value', o); closeList(); });
        ul.appendChild(li);
      });
      document.body.appendChild(ul);
      return;
    }
    if (t.hasAttribute('data-template')) {
      var tpl = document.getElementById(t.getAttribute('data-template'));
      var box = t.parentElement.querySelector('.entries');
      var n = box.children.length + 1;
      var holder = document.createElement('div');
      holder.innerHTML = tpl.innerHTML.split('__N__').join(String(n));
      while (holder.firstChild) { box.appendChild(holder.firstChild); }
      return;
    }
    if (t.hasAttribute('data-next')) { location.href = t.getAttribute('data-next'); return; }
    if (t.hasAttribute('data-submit')) { fetch('/submitted', { method: 'POST' }); return; }
  });
  document.addEventListener('keydown', function (ev) {
    var box = ev.target;
    if (ev.key !== 'Enter' || !box.matches || !box.matches('input[data-automation-id="searchBox"]')) { return; }
    ev.preventDefault();
    closeList();
    var q = (box.value || '').toLowerCase();
    var list = document.createElement('div');
    list.setAttribute('data-automation-id', 'activeListContainer');
    optionsOf(box).filter(function (o) { return o.toLowerCase().indexOf(q) >= 0; }).forEach(function (o) {
      var d = document.createElement('div');
      d.setAttribute('role', 'option');
      d.setAttribute('data-automation-id', 'promptOption');
      d.textContent = o;
      d.addEventListener('click', function () {
        var chips = box.closest('[data-automation-id="multiSelectContainer"]').querySelector('[data-automation-id="selectedItemList"]');
        var chip = document.createElement('div');
        chip.setAttribute('data-automation-id', 'selectedItem');
        var s = document.createElement('span');
        s.setAttribute('data-automation-id', 'promptOption');
        s.textContent = o;
        chip.appendChild(s);
        chips.appendChild(chip);
        box.value = '';
        closeList();
      });
      list.appendChild(d);
    });
    if (list.children.length) { document.body.appendChild(list); }
  });
  document.addEventListener('change', function (ev) {
    var c = ev.target;
    if (c.matches && c.matches('input[data-automation-id="currentlyWorkHere"]')) {
      var group = c.closest('[data-automation-id^="workExperience-"]');
      var end = group && group.querySelector('[data-automation-id="formField-endDate"]');
      if (end) { end.style.display = c.checked ? 'none' : ''; }
    }
  });
})();
