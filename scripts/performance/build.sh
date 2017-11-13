#!/usr/bin/env bash

rm -rf ./artifacts
. env/bin/activate

export TRAVIS_BUILD_NUMBER=`date +%s`
TARGET_DIR=./artifacts/perf/$TRAVIS_BUILD_NUMBER
./scripts/ci/build.sh

mkdir -p $TARGET_DIR
cp ./artifacts/build/* $TARGET_DIR
rm $TARGET_DIR/azure_cli_fulltest*
cp ./scripts/performance/measure.py $TARGET_DIR
cp ./scripts/performance/install.sh $TARGET_DIR

cd $TARGET_DIR
tar cvf ../perf.tar .
cd ..

# assume the storage account and key are set through the environment variable
az storage blob upload -c perf -n perf.tar -f perf.tar

