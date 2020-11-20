#!/usr/bin/env bash
# update API endpoint with new model, code, etc.

APP_LBL='api-endpoint'  # descriptive label for endpoint-related directories
REPO_LBL='spd'  # directory where repo code will go
ETC_PATH="/etc/${APP_LBL}"  # app config info, scripts, ML models, etc.
SRV_PATH="/srv/${APP_LBL}"  # application resources for serving endpoint
TMP_PATH="/tmp/${APP_LBL}"  # store temporary files created as part of setting up app (cleared with every update)
LIB_PATH="/var/lib/${APP_LBL}"  # where virtualenv will sit

# clean up old versions
rm -rf ${TMP_PATH}
mkdir -p ${TMP_PATH}

mv ${REPO_LBL} ${TMP_PATH}/${REPO_LBL}

# reinstall virtualenv
rm -rf ${LIB_PATH}/p3env
echo "Setting up virtualenv..."
python3 -m venv ${LIB_PATH}/p3env
source ${LIB_PATH}/p3env/bin/activate

echo "Installing repositories..."
pip install wheel
pip install -r ${TMP_PATH}/${REPO_LBL}/requirements.txt

# update config / code -- if only changing Python and not nginx/uwsgi code, then much of this can be commented out
# the necessary files need to be manually scp-ed onto the server but ideally this is a simple git pull command
echo "Copying configuration files..."
cp ${TMP_PATH}/${REPO_LBL}/model/config/* ${ETC_PATH}
cp ${TMP_PATH}/${REPO_LBL}/model/templates/* ${ETC_PATH}/templates
cp ${TMP_PATH}/${REPO_LBL}/model/static/* ${ETC_PATH}/static
cp ${TMP_PATH}/${REPO_LBL}/model/wsgi.py ${ETC_PATH}
cp ${TMP_PATH}/${REPO_LBL}/model/flask_config.yaml ${ETC_PATH}
chmod 400 ${ETC_PATH}/flask_config.yaml
cp ${ETC_PATH}/model.nginx /etc/nginx/sites-available/model
if [[ -f "/etc/nginx/sites-enabled/model" ]]; then
    unlink /etc/nginx/sites-enabled/model
fi
ln -s /etc/nginx/sites-available/model /etc/nginx/sites-enabled/
cp ${ETC_PATH}/model.service /etc/systemd/system/

echo "Enabling and starting services..."
systemctl enable model.service  # uwsgi starts when server starts up
systemctl daemon-reload  # refresh state

systemctl restart model.service  # start up uwsgi
systemctl restart nginx  # start up nginx