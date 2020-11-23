venv: requirements.txt
	test -d venv || virtualenv venv
	. venv/bin/activate; pip install -Ur requirements.txt

make_dockerfile: .pipeline/blubber.yaml
	blubber .pipeline/blubber.yaml build > Dockerfile

docker: make_dockerfile
	docker build -t sockpuppet  .
	docker run -p 5000:5000 sockpuppet
