import unittest
from kakarot_daemon.security import requires_hitl

class TestClassifier(unittest.TestCase):
    def test_passes(self):
        self.assertFalse(requires_hitl("df -h"))
        self.assertFalse(requires_hitl("cat /tmp/test.txt"))
        self.assertFalse(requires_hitl("systemctl status kakarot.service"))
        self.assertFalse(requires_hitl("ls -la --color=auto"))
        self.assertFalse(requires_hitl("journalctl -u kakarot.service"))
        
    def test_fails(self):
        self.assertTrue(requires_hitl("rm -rf /"))
        self.assertTrue(requires_hitl("ls -la | grep foo"))
        self.assertTrue(requires_hitl("echo 'foo' > /tmp/bar"))
        self.assertTrue(requires_hitl("cat foo.txt && rm foo.txt"))
        self.assertTrue(requires_hitl("systemctl restart docker"))
        self.assertTrue(requires_hitl('cat "unclosed_quote_file'))

if __name__ == '__main__':
    unittest.main()
