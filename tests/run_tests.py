#!/usr/bin/env python3
"""
Test runner for pcat-manager-web firmware upgrade tests

This script runs the firmware upgrade unit tests with proper mocking
to avoid executing real system commands.
"""

import os
import sys
import unittest
from unittest.mock import patch

# Add the project root to Python path
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(project_root, 'app'))

def main():
    """Main test runner function"""
    print("Running pcat-manager-web firmware upgrade tests...")
    print("=" * 60)
    
    # Mock critical imports that might not be available in test environment
    with patch.dict('sys.modules', {
        'pcat_logger': unittest.mock.MagicMock(),
        'socketio': unittest.mock.MagicMock(),
        'pcat_config': unittest.mock.MagicMock(),
    }):
        
        # Discover and run tests
        loader = unittest.TestLoader()
        start_dir = os.path.dirname(os.path.abspath(__file__))
        suite = loader.discover(start_dir, pattern='test_*.py')
        
        # Run tests with verbose output
        runner = unittest.TextTestRunner(
            verbosity=2,
            failfast=False,
            buffer=True
        )
        
        result = runner.run(suite)
        
        # Print summary
        print("\n" + "=" * 60)
        if result.wasSuccessful():
            print("✅ All tests passed!")
            print(f"Tests run: {result.testsRun}")
        else:
            print("❌ Some tests failed!")
            print(f"Tests run: {result.testsRun}")
            print(f"Failures: {len(result.failures)}")
            print(f"Errors: {len(result.errors)}")
            
            if result.failures:
                print("\nFailures:")
                for test, traceback in result.failures:
                    error_msg = traceback.split('AssertionError: ')[-1].split('\n')[0]
                    print(f"  - {test}: {error_msg}")
                    
            if result.errors:
                print("\nErrors:")
                for test, traceback in result.errors:
                    error_line = traceback.split('\n')[-2]
                    print(f"  - {test}: {error_line}")
        
        return result.wasSuccessful()

if __name__ == '__main__':
    success = main()
    sys.exit(0 if success else 1)