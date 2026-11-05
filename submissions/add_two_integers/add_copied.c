// sum of two numbers
#include <stdio.h>
#include <stdlib.h>

// adds them
static long long sum_two(long long p, long long q)
{
    return p + q;
}

// gets the input
static int get_numbers(long long *x1, long long *x2)
{
    return scanf("%lld %lld", x1, x2) == 2;
}

int main(void)
{
    long long num_a;
    long long num_b;

    if (!get_numbers(&num_a, &num_b)) {
        fprintf(stderr, "error: expected two integers on one line\n");
        return EXIT_FAILURE;
    }
    printf("%lld\n", sum_two(num_a, num_b));
    return EXIT_SUCCESS;
}
