#!/bin/bash

# Clean up previous test results
rm -rf test_results

# Function to convert file name to test name
convert_filename_to_testname() {
    local filename="$1"
    
    # Remove test_ prefix and .py suffix if present
    filename="${filename#test_}"
    filename="${filename%.py}"
    
    # Add _test suffix if not present
    if [[ ! "$filename" =~ _test$ ]]; then
        filename="${filename}_test"
    fi
    
    echo "$filename"
}

# Function to run specific tests
run_specific_tests() {
    local test_args=()
    
    for arg in "$@"; do
        # If it looks like a filename (contains test_ or ends with .py), convert it
        if [[ "$arg" == test_* ]] || [[ "$arg" == *.py ]]; then
            converted=$(convert_filename_to_testname "$arg")
            test_args+=("$converted")
        else
            # Otherwise, assume it's already a test name
            # Add _test suffix if not present
            if [[ ! "$arg" =~ _test$ ]]; then
                test_args+=("${arg}_test")
            else
                test_args+=("$arg")
            fi
        fi
    done
    
    echo "🧪 Running specific tests: ${test_args[*]}"
    python run_tests.py run-hierarchical-tests "${test_args[@]}"
}

# Main logic
if [ $# -eq 0 ]; then
    # No arguments provided, run all tests
    echo "🚀 Running all tests with dependency resolution..."
    python run_tests.py run-hierarchical
else
    # Arguments provided, run specific tests
    run_specific_tests "$@"
fi