function authFetch(url, options = {}) {
    const token = localStorage.getItem("token");
    // 如果没有 token，直接跳转到登录页
    if (!token) {
        window.location.href = "/account/login";
        // 阻止后续请求
        return Promise.reject(new Error("No token, redirecting to login"));
    }
    const headers = options.headers || {};
    return fetch(url, {
        ...options,
        headers: {
            ...headers,
            Authorization: `Bearer ${token}`
        }
    }).then(resp => {
		if (resp.status === 401) {
			window.location.href = "/account/login";
		}
		return resp;
	});
}
