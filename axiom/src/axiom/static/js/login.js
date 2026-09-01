/* ═══════════════════════════════════════════════════════════════════════
 * AXIOM 登录网关 login.js —— 唯一对未登录访问者公开的业务脚本
 *
 * 安全设计（方案1：业务 JS 鉴权隔离 + 服务端按登录态提供 HTML）：
 *   - 未登录 GET / 时，服务端中间件只放行 login.js（与登录卡必需资源）。
 *   - 全部业务脚本（state/core/projects/.../app.js）与页面模板由中间件保护，
 *     未认证请求一律返回 404；攻击者无法通过下载 JS 枚举内部 API 路径。
 *   - 本文件仅包含登录/登出/改密/状态检测的最小逻辑；
 *     登录成功或携带有效会话时，location.reload() 让服务端按已登录态返回
 *     完整 HTML（含全部业务脚本），Alpine 完整 fresh 初始化。
 * ═══════════════════════════════════════════════════════════════════════ */
(function () {
  "use strict";

  window.AxiomLogin = {
    /* 登录成功/已登录时调用：让服务端按已登录态返回完整 HTML。 */
    refresh() { window.location.reload(); },
  };

  /* 占位 axiomApp：登录卡阶段所需的最小状态与方法。
   * app.js 在已登录 HTML 中会重新声明完整 axiomApp()，本占位仅作兜底。 */
  window.axiomApp = function axiomApp() {
    return {
      authLoading: true,
      isAuthenticated: false,
      currentUser: null,
      loginForm: { username: "", password: "", remember: true, error: "", loading: false },
      changePasswordDialog: { open: false, oldPassword: "", newPassword: "", confirmPassword: "", error: "", success: "", loading: false },
      toast: { show: false, message: "", type: "info" },

      async init() {
        // 检测已有会话（localStorage token）。有效则 reload 触发完整应用加载。
        var ok = await this.checkAuthStatus();
        if (ok) { window.AxiomLogin.refresh(); }
      },

      async checkAuthStatus() {
        try {
          var token = null;
          try { token = localStorage.getItem("axiom_token"); } catch (e) {}
          var headers = token ? { "Authorization": "Bearer " + token } : {};
          var resp = await fetch("/api/v1/auth/status", { headers: headers });
          if (resp.ok) {
            var data = await resp.json();
            if (data.authenticated) {
              this.isAuthenticated = true;
              this.currentUser = data.username;
              return true;
            }
          }
        } catch (e) {
          console.error("Failed to check auth status:", e);
        }
        this.isAuthenticated = false;
        this.currentUser = null;
        this.authLoading = false;
        return false;
      },

      async performLogin() {
        if (!this.loginForm.username || !this.loginForm.password) {
          this.loginForm.error = "请输入用户名和密码";
          return;
        }
        this.loginForm.loading = true;
        this.loginForm.error = "";
        try {
          var resp = await fetch("/api/v1/auth/login", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              username: this.loginForm.username,
              password: this.loginForm.password,
              remember: this.loginForm.remember,
            }),
          });
          var data = await resp.json();
          if (!resp.ok) {
            throw new Error((data.error && data.error.message) || "登录失败，请检查账号密码");
          }
          try { localStorage.setItem("axiom_token", data.token); } catch (e) {}
          this.isAuthenticated = true;
          this.currentUser = data.username;
          this.loginForm.password = "";
          this.showToast("登录成功，正在加载控制台…");
          // 让服务端按已登录态返回完整 HTML（Alpine fresh 初始化，无破坏风险）
          window.AxiomLogin.refresh();
        } catch (err) {
          this.loginForm.error = err.message || "登录失败";
          this.loginForm.loading = false;
        }
      },

      async performLogout() {
        try { await fetch("/api/v1/auth/logout", { method: "POST" }); } catch (e) {}
        try { localStorage.removeItem("axiom_token"); } catch (e) {}
        window.AxiomLogin.refresh();
      },

      openChangePasswordDialog() {
        this.changePasswordDialog = {
          open: true,
          oldPassword: "", newPassword: "", confirmPassword: "",
          error: "", success: "", loading: false,
        };
      },

      async submitChangePassword() {
        var d = this.changePasswordDialog;
        if (!d.oldPassword) { d.error = "请输入原密码"; return; }
        if (!d.newPassword) { d.error = "请输入新密码"; return; }
        if (d.newPassword.length < 6) { d.error = "新密码长度至少需要 6 位"; return; }
        if (d.newPassword !== d.confirmPassword) { d.error = "两次输入的新密码不一致"; return; }
        d.loading = true;
        d.error = ""; d.success = "";
        try {
          var token = null;
          try { token = localStorage.getItem("axiom_token"); } catch (e) {}
          var resp = await fetch("/api/v1/auth/change-password", {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "Authorization": token ? "Bearer " + token : "",
            },
            body: JSON.stringify({ old_password: d.oldPassword, new_password: d.newPassword }),
          });
          var data = await resp.json();
          if (!resp.ok) {
            throw new Error((data.error && data.error.message) || "修改密码失败");
          }
          if (data.token) {
            try { localStorage.setItem("axiom_token", data.token); } catch (e) {}
          }
          d.success = "密码修改成功！请妥善保管新密码";
          this.showToast("密码修改成功");
          var self = this;
          setTimeout(function () { self.changePasswordDialog.open = false; }, 1200);
        } catch (err) {
          d.error = err.message || "修改失败";
        } finally {
          d.loading = false;
        }
      },

      showToast(msg, type) {
        var self = this;
        this.toast = { show: true, message: msg, type: type || "info" };
        setTimeout(function () { self.toast.show = false; }, 3000);
      },
    };
  };
})();
