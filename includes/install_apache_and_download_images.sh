#!/bin/bash

apt-get update
apt-get -y install wget apache2
cd /var/www/html/
rm index.html
wget https://www.dropbox.com/s/hpuq1apmtt2s9g9/BIGIP-11.5.3.2.10.196.LTM_1SLOT.qcow2.zip &
wget https://www.dropbox.com/s/dp3i7901w3lusje/BIGIP-11.5.3.2.10.196.LTM.qcow2.zip &
wget https://www.dropbox.com/s/q7l14qw9we7cula/BIGIP-11.5.3.2.10.196.ALL.qcow2.zip &
wget https://www.dropbox.com/s/qsoe8afhi8pdcy6/BIGIP-11.6.0.0.0.401.LTM_1SLOT.qcow2.zip &
wget https://www.dropbox.com/s/5v756vb7wzm8d4j/BIGIP-11.6.0.0.0.401.LTM.qcow2.zip &
wget https://www.dropbox.com/s/7ad3ze9w5a88qfi/BIGIP-11.6.0.0.0.401.ALL.qcow2.zip &
wget https://www.dropbox.com/s/siio6nnm9vbi973/BIGIP-12.0.0.0.0.606.LTM_1SLOT.qcow2.zip &
wget https://www.dropbox.com/s/0z2sk1vn1yvg20t/BIGIP-12.0.0.0.0.606.LTM.qcow2.zip &
wget https://www.dropbox.com/s/8cg5xldjl9f6yz5/BIGIP-12.0.0.0.0.606.ALL.qcow2.zip &
wget https://www.dropbox.com/s/pxwr5aaf5y55zg4/BIG-IQ-4.5.0.0.0.7028.qcow2.zip &




