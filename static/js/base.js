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
