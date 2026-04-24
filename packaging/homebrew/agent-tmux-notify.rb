class AgentTmuxNotify < Formula
  include Language::Python::Virtualenv

  desc "Monitor Claude Code CLI instances inside tmux panes"
  homepage "https://github.com/xbunax/agent-tmux-notify"
  url "https://github.com/xbunax/agent-tmux-notify/archive/refs/tags/v0.1.1.tar.gz"
  sha256 "0491a8de5e9a8c9426b91dbfc6b2975c7452b8f92c71749458aa3cac40bbd062"
  license "Apache-2.0"
  revision 1

  depends_on "python@3.13"

  resource "markdown-it-py" do
    url "https://files.pythonhosted.org/packages/5b/f5/4ec618ed16cc4f8fb3b701563655a69816155e79e24a17b651541804721d/markdown_it_py-4.0.0.tar.gz"
    sha256 "cb0a2b4aa34f932c007117b194e945bd74e0ec24133ceb5bac59009cda1cb9f3"
  end

  resource "mdurl" do
    url "https://files.pythonhosted.org/packages/d6/54/cfe61301667036ec958cb99bd3efefba235e65cdeb9c84d24a8293ba1d90/mdurl-0.1.2.tar.gz"
    sha256 "bb413d29f5eea38f31dd4754dd7377d4465116fb207585f97bf925588687c1ba"
  end

  resource "pygments" do
    url "https://files.pythonhosted.org/packages/c3/b2/bc9c9196916376152d655522fdcebac55e66de6603a76a02bca1b6414f6c/pygments-2.20.0.tar.gz"
    sha256 "6757cd03768053ff99f3039c1a36d6c0aa0b263438fcab17520b30a303a82b5f"
  end

  resource "rich" do
    url "https://files.pythonhosted.org/packages/c0/8f/0722ca900cc807c13a6a0c696dacf35430f72e0ec571c4275d2371fca3e9/rich-15.0.0.tar.gz"
    sha256 "edd07a4824c6b40189fb7ac9bc4c52536e9780fbbfbddf6f1e2502c31b068c36"
  end

  def install
    virtualenv_install_with_resources
  end

  service do
    run [opt_bin/"agent-tmux-notify"]
    keep_alive true
    run_type :immediate
    environment_variables PATH: std_service_path_env, LANG: "en_US.UTF-8"
    log_path var/"log/agent-tmux-notify.log"
    error_log_path var/"log/agent-tmux-notify.error.log"
  end

  test do
    output = shell_output("#{bin}/agent-tmux-notify --help")
    assert_match "agent-tmux-notify", output
  end
end
