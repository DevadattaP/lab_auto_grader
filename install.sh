#!/bin/bash
set -e

# Lab Auto-Grader Installation Script
# This script sets up the lab auto-grader platform with:
# - Python environment and dependencies
# - isolate sandbox (with cgroup v2 setup)
# - Admin account initialization
# - Quick-start instructions

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Helper functions
print_status() {
    echo -e "${GREEN}[✓]${NC} $1"
}

print_error() {
    echo -e "${RED}[✗]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[!]${NC} $1"
}

print_step() {
    echo -e "\n${GREEN}==>${NC} $1"
}

# Check if running as sudo for privileged operations
check_sudo() {
    if [[ $EUID -ne 0 ]]; then
        print_warning "This script needs root access for some operations. You'll be prompted for sudo when needed."
    fi
}

# Check OS and cgroup setup
check_os_and_cgroups() {
    print_step "Checking OS and cgroup v2 setup"

    if [[ ! -f /etc/os-release ]]; then
        print_error "Cannot determine OS"
        exit 1
    fi

    source /etc/os-release
    print_status "OS: $PRETTY_NAME"

    # Check cgroup v2
    if [[ -f /sys/fs/cgroup/cgroup.controllers ]]; then
        print_status "cgroup v2 detected (required)"
    else
        cgroup_type=$(stat -fc %T /sys/fs/cgroup 2>/dev/null || echo "unknown")
        if [[ "$cgroup_type" == "cgroup2fs" ]]; then
            print_status "cgroup v2 unified hierarchy detected"
        else
            print_error "cgroup v2 unified hierarchy NOT detected"
            print_warning "cgroup type: $cgroup_type"
            print_warning "isolate requires cgroup v2. If on legacy v1 or hybrid cgroup, you need:"
            print_warning "  1. Add 'systemd.unified_cgroup_hierarchy=1' to GRUB kernel command line"
            print_warning "  2. Run: sudo update-grub && sudo reboot"
            echo ""
            read -p "Continue anyway? (y/n) " -n 1 -r
            echo
            if [[ ! $REPLY =~ ^[Yy]$ ]]; then
                exit 1
            fi
        fi
    fi
}

# Check and install system dependencies
check_and_install_deps() {
    print_step "Checking system dependencies"

    local deps_needed=0

    # Check Python 3
    if ! command -v python3 &> /dev/null; then
        print_warning "Python 3 not found - will install"
        deps_needed=1
    else
        print_status "Python 3 found: $(python3 --version)"
    fi

    # Check git (needed for isolate)
    if ! command -v git &> /dev/null; then
        print_warning "git not found - will install"
        deps_needed=1
    else
        print_status "git found"
    fi

    # Check build tools (needed for isolate)
    if ! command -v gcc &> /dev/null; then
        print_warning "build-essential not found - will install"
        deps_needed=1
    else
        print_status "build-essential found"
    fi

    if [[ $deps_needed -eq 1 ]]; then
        print_step "Installing system dependencies"
        sudo apt update
        sudo apt install -y \
            python3 python3-venv python3-pip \
            build-essential libseccomp-dev libcap-dev pkg-config \
            git libsystemd-dev asciidoc-base
        print_status "System dependencies installed"
    else
        print_status "All system dependencies found"
    fi
}

# Check if isolate is already installed
check_isolate() {
    if command -v isolate &> /dev/null; then
        print_status "isolate already installed: $(isolate --version 2>/dev/null || echo 'installed')"
        read -p "Skip isolate installation? (y/n) " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            return 1
        fi
    fi
    return 0
}

# Install isolate from source
install_isolate() {
    if ! check_isolate; then
        return
    fi

    print_step "Installing isolate sandbox"

    # Create temp directory for build
    local tmpdir=$(mktemp -d)
    cd "$tmpdir"

    print_status "Cloning isolate from GitHub..."
    git clone https://github.com/ioi/isolate.git
    cd isolate

    print_status "Building isolate..."
    make isolate isolate-cg-keeper

    print_status "Installing isolate (requires sudo)..."
    sudo make install

    # Post-install setup
    print_step "Post-install setup (requires sudo)"

    # 1. Create isolate system user
    if ! id isolate &>/dev/null; then
        print_status "Creating 'isolate' system user..."
        sudo useradd -r -s /usr/sbin/nologin isolate
    else
        print_status "'isolate' user already exists"
    fi

    # 2. Setup subuid/subgid
    print_status "Setting up subuid/subgid..."
    if ! grep -q "^isolate:" /etc/subuid; then
        echo "isolate:200000:65536" | sudo tee -a /etc/subuid > /dev/null
        echo "isolate:200000:65536" | sudo tee -a /etc/subgid > /dev/null
    else
        print_status "subuid/subgid already configured"
    fi

    # 3. Setup systemd units for cgroup delegation
    print_status "Setting up systemd cgroup delegation..."
    sudo cp systemd/isolate.slice /etc/systemd/system/
    sudo sed "s#@SBINDIR@#/usr/local/sbin#" \
        systemd/isolate.service.in | sudo tee /etc/systemd/system/isolate.service > /dev/null

    sudo systemctl daemon-reload
    sudo systemctl enable --now isolate.service

    # 4. Setup setuid-root
    print_status "Setting up setuid-root for isolate..."
    sudo chown root:root /usr/local/bin/isolate
    sudo chmod u+s /usr/local/bin/isolate

    # Test isolate
    print_step "Testing isolate installation"
    if isolate --box-id=0 --cg --init && isolate --box-id=0 --cg --cleanup; then
        print_status "isolate smoke test passed"
    else
        print_error "isolate smoke test failed"
        print_warning "Check /etc/isolate/default config and isolate.service status"
        exit 1
    fi

    # Cleanup
    cd /
    rm -rf "$tmpdir"
    print_status "isolate installation complete"
}

# Setup Python virtual environment
setup_python_venv() {
    print_step "Setting up Python virtual environment"

    local venv_dir="venv"

    if [[ -d "$venv_dir" ]]; then
        print_status "venv directory already exists"
        read -p "Recreate venv? (y/n) " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            rm -rf "$venv_dir"
        else
            return
        fi
    fi

    python3 -m venv "$venv_dir"
    source "$venv_dir/bin/activate"
    print_status "Virtual environment created and activated"

    # Upgrade pip
    pip install --upgrade pip setuptools wheel > /dev/null

    # Install requirements
    print_status "Installing Python dependencies from requirements.txt..."
    if [[ -f "requirements.txt" ]]; then
        pip install -r requirements.txt
        print_status "Python dependencies installed"
    else
        print_error "requirements.txt not found in current directory"
        exit 1
    fi
}

# Create necessary directories
create_directories() {
    print_step "Creating project directories"

    for dir in questions submissions runs live; do
        if [[ ! -d "$dir" ]]; then
            mkdir -p "$dir"
            print_status "Created $dir/"
        else
            print_status "$dir/ already exists"
        fi
    done
}

# Initialize admin account
init_admin_account() {
    print_step "Initializing admin account"

    # Check if .env already exists
    if [[ -f ".env" ]]; then
        print_status ".env file already exists"
        read -p "Reinitialize admin account? (y/n) " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            return
        fi
    fi

    source venv/bin/activate
    python3 -m grader.manage_accounts init-admin
    print_status "Admin account initialized"
}

# Print usage instructions
print_usage_instructions() {
    print_step "Installation Complete!"

    cat << 'EOF'

Next steps:

1. ACTIVATE VIRTUAL ENVIRONMENT (every time you start work):
   source venv/bin/activate

2. AUTHOR QUESTIONS:
   Create question definitions in questions/lab_01/, questions/lab_02/, etc.
   See AUTOGRADER_DESIGN.md for the YAML format.

3. FOR OFFLINE GRADING:
   python -m grader.grade \
     --questions questions/lab_01 \
     --submissions submissions/lab_01 \
     --out runs/lab_01 \
     --sandbox isolate \
     --parallel 4

4. FOR LIVE LAB SESSIONS:

   a) (Optional) Generate student accounts (one-time per lab):
      python -m grader.manage_accounts generate \
        --lab lab_01 \
        --roster roster.txt

   b) Start student server (http://<lab-pc-ip>:5001):
      python -m server_student.app --lab lab_01 --port 5001

   c) Start admin server (http://127.0.0.1:5002):
      python -m server_admin.app --port 5002
      Login at http://127.0.0.1:5002/admin

5. VIEW WEB UI (offline mode, question/submission browser):
   python -m ui.app
   Open http://127.0.0.1:5000/

DOCUMENTATION:
   - AUTOGRADER_DESIGN.md — Full design, threat model, offline grading pipeline
   - LIVE_LAB_DESIGN.md — Live platform architecture, authentication, security
   - README.md — Day-to-day reference, examples

TROUBLESHOOTING:
   - isolate smoke test: isolate --box-id=0 --cg --init && isolate --box-id=0 --cg --cleanup
   - Check isolate.service: sudo systemctl status isolate.service
   - Check cgroup v2: stat -fc %T /sys/fs/cgroup (should show cgroup2fs)

EOF
}

# Main installation flow
main() {
    clear
    echo -e "${GREEN}"
    cat << 'EOF'
╔════════════════════════════════════════════════════════════╗
║     Lab Auto-Grader Interactive Installation Script        ║
║                                                            ║
║  Compile & grade C code, run live coding sessions,         ║
║  manage students, and generate reports.                    ║
╚════════════════════════════════════════════════════════════╝
EOF
    echo -e "${NC}"

    # Verify we're in the right directory
    if [[ ! -f "requirements.txt" ]] || [[ ! -f "AUTOGRADER_DESIGN.md" ]]; then
        print_error "This script must be run from the lab_auto_grader root directory"
        exit 1
    fi

    print_status "Running from: $(pwd)"

    # Run checks and installations
    check_sudo
    check_os_and_cgroups
    check_and_install_deps
    install_isolate
    setup_python_venv
    create_directories
    init_admin_account
    print_usage_instructions

    print_step "Installation Summary"
    print_status "All components installed successfully!"
    print_status "Run 'source venv/bin/activate' to start using the grader"
}

# Run main if script is executed directly
main
