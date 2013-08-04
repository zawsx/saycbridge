.PHONY: all clean baseline baseline-z3 accept accept-z3 check check-z3 compile serve publish chromeapp

src_dir = src

appengine_dir = dist/gae
appengine_scripts_dir = $(appengine_dir)/scripts

THIRD_PARTY_SCRIPTS = \
	third_party/jquery-1.6.2.min.js \
	third_party/jquery.history.js \
	third_party/bignumber.js \

SCRIPTS = \
	model.js \
	controller.js \
	view.js \
	controller.js \
	controller.test.js \
	new_bidder.js \
	play.js \
	play.test.js \
	recap.js \

all:
	@echo "Run 'make baseline' to create baselines and 'make check' to diff the current results."

clean:
	@find . -name "*.pyc" | xargs rm

accept:
	@mv $(src_dir)/z3b_actual.txt $(src_dir)/z3b_baseline.txt

check: clean
	@$(src_dir)/test-sayc > $(src_dir)/z3b_actual.txt ; true
	@diff -U 7 $(src_dir)/z3b_baseline.txt $(src_dir)/z3b_actual.txt ; true


# Support for the old Knowledge Based Bidder:

accept-kbb:
	@mv $(src_dir)/kbb_actual.txt $(src_dir)/kbb_baseline.txt

check-kbb: clean
	@$(src_dir)/test-sayc -k > $(src_dir)/kbb_actual.txt ; true
	@diff -U 7 $(src_dir)/kbb_baseline.txt $(src_dir)/kbb_actual.txt ; true

compile:
	coffee --compile $(appengine_scripts_dir)/*.coffee

# FIXME: Currently there is no way to run the site with the z3 bidder.
# AppEngine runs in a hermetic python environment which is incapable
# of running z3.
serve-kbb: clean
	coffee --watch --compile $(appengine_scripts_dir)/*.coffee &
	python2.7 `which dev_appserver.py` $(appengine_dir)

publish: compile
	@appcfg.py --oauth2 --email=macdome@gmail.com update $(appengine_dir)

# FIXME: Need some way to make this work from a macro instead of an explicit list of files.
closure:
	java -jar compiler.jar \
		--js=$(appengine_scripts_dir)/third_party/jquery-1.6.2.min.js \
		--js=$(appengine_scripts_dir)/third_party/jquery.history.js \
		--js=$(appengine_scripts_dir)/third_party/bignumber.js \
		--js=$(appengine_scripts_dir)/model.js \
		--js=$(appengine_scripts_dir)/controller.js \
		--js=$(appengine_scripts_dir)/view.js \
		--js=$(appengine_scripts_dir)/play.js \
		--js_output_file=bidder.js

chromeapp:
	zip -r bridge.zip dist/crx/*
