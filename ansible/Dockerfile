FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV UBUNTU_CODENAME=jammy

# Install required base packages
RUN apt-get update && apt-get install -y \
    gnupg wget software-properties-common curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Add Ansible PPA key and repository
RUN wget -O- "https://keyserver.ubuntu.com/pks/lookup?fingerprint=on&op=get&search=0x6125E2A8C77F2818FB7BD15B93C4A3FD7BB9C367" \
    | gpg --dearmour -o /usr/share/keyrings/ansible-archive-keyring.gpg \
 && echo "deb [signed-by=/usr/share/keyrings/ansible-archive-keyring.gpg] http://ppa.launchpad.net/ansible/ansible/ubuntu ${UBUNTU_CODENAME} main" \
    > /etc/apt/sources.list.d/ansible.list

# Install Ansible
RUN apt-get update && apt-get install -y ansible \
 && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /ansible

# Default shell
ENTRYPOINT ["bash"]
