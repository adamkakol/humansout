from slugify import slugify

assert slugify("Hello, World!") == "hello-world"
assert slugify("  many   spaces  ") == "many-spaces"
print("visible tests passed")
