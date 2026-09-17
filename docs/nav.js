// Mobile menu for the docs sidebar. Without this script the sidebar
// simply stacks above the content, so nothing depends on it running.
(function () {
  var root = document.documentElement;
  var button = document.querySelector("header.site .menu");
  var nav = document.getElementById("site-nav");
  if (!button || !nav) { return; }
  root.classList.add("js");

  function setOpen(open) {
    document.body.classList.toggle("nav-open", open);
    button.setAttribute("aria-expanded", open ? "true" : "false");
    button.textContent = open ? "Close" : "Menu";
  }

  button.addEventListener("click", function () {
    var open = button.getAttribute("aria-expanded") !== "true";
    setOpen(open);
    if (open) {
      var first = nav.querySelector("a");
      if (first) { first.focus(); }
    }
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && button.getAttribute("aria-expanded") === "true") {
      setOpen(false);
      button.focus();
    }
  });

  setOpen(false);
})();
