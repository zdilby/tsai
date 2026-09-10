function authFetch(url, options = {}) {
    return fetch(url, {
        ...options,
        credentials: "include",
    }).then(resp => {
		if (resp.status === 401) {
			window.location.href = "/account/login";
		}
		return resp;
	});
}

// 公共：侧栏底部「切换模块」向上弹出菜单（_module_switch.html），各模块页共用。
// 不用 Materialize Dropdown —— 它在 position:absolute 的侧栏底部会被裁切/错位。
document.addEventListener("click", function (e) {
	var btn = e.target.closest && e.target.closest(".module-switch-btn");
	var openWrap = document.querySelector(".module-switch.open");
	if (btn) {
		e.preventDefault();
		var wrap = btn.closest(".module-switch");
		var wasOpen = wrap.classList.contains("open");
		if (openWrap) openWrap.classList.remove("open");
		if (!wasOpen) wrap.classList.add("open");
		return;
	}
	if (openWrap && !(e.target.closest && e.target.closest(".module-switch-menu"))) {
		openWrap.classList.remove("open");
	}
});
